import os
import calendar
import tempfile
import uuid
import datetime as dt
from celery import group
from celery.result import AsyncResult
from datetime import datetime, timedelta
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from django.db import transaction
from django.db.models import Q, Count
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response
from rest_framework.views import APIView
from organizations.models import Organization
from ..models import (
    ClientUser,
    Job,
    Candidate,
    EngagementTemplates,
    InterviewerAvailability,
    Engagement,
    EngagementOperation,
    Interview,
    BillingRecord,
    BillingLog,
    BillPayments,
    DesignationDomain,
)
from ..serializer import (
    ClientUserSerializer,
    JobSerializer,
    CandidateSerializer,
    EngagementTemplateSerializer,
    EngagementSerializer,
    EngagementOperationSerializer,
    EngagementUpdateStatusSerializer,
    EngagmentOperationStatusUpdateSerializer,
    FinanceSerializer,
    AnalyticsQuerySerializer,
    FeedbackPDFVideoSerializer,
    FinanceSerializerForInterviewer,
)
from ..permissions import CanDeleteUpdateUser, UserRoleDeleteUpdateClientData
from externals.parser.resumeparser2 import process_resumes
from externals.analytics import get_candidate_analytics
from externals.payment.cashfree import create_payment_link, is_valid_signature
from core.permissions import (
    IsClientAdmin,
    IsClientOwner,
    IsClientUser,
    IsAgency,
    HasRole,
    IsSuperAdmin,
    IsAdmin,
    IsModerator,
    IsInterviewer,
)
from core.models import Role, User
from hiringdogbackend.utils import validate_attachment
from ..tasks import send_schedule_engagement_email


@extend_schema(tags=["Client"])
class ClientUserView(APIView, LimitOffsetPagination):
    serializer_class = ClientUserSerializer
    permission_classes = [IsAuthenticated, HasRole, CanDeleteUpdateUser]
    roles_mapping = {
        "GET": [Role.CLIENT_ADMIN, Role.CLIENT_OWNER, Role.CLIENT_USER, Role.AGENCY],
        "POST": [Role.CLIENT_ADMIN, Role.CLIENT_OWNER],
        "PATCH": [Role.CLIENT_ADMIN, Role.CLIENT_OWNER],
        "DELETE": [Role.CLIENT_ADMIN, Role.CLIENT_OWNER],
    }

    def get(self, request, **kwargs):
        organization = request.user.clientuser.organization
        client_users = ClientUser.objects.filter(
            organization=organization
        ).select_related("user")

        if request.user.role == Role.CLIENT_USER:
            client_user = client_users.filter(user=request.user).first()
            serializer = self.serializer_class(client_user)
            return Response(
                {
                    "status": "success",
                    "message": "Client User retrieved successfully",
                    "data": serializer.data,
                }
            )

        client_users = client_users.prefetch_related("jobs")
        paginated_client_users = self.paginate_queryset(client_users, request)
        serializer = self.serializer_class(paginated_client_users, many=True)
        paginated_data = self.get_paginated_response(serializer.data)
        return Response(
            {
                "status": "success",
                "message": "Client users retrieved successfully.",
                **paginated_data.data,
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request, **kwargs):
        serializer = self.serializer_class(
            data=request.data, context={"user": request.user}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(
            invited_by=request.user, organization=request.user.clientuser.organization
        )
        return Response(
            {
                "status": "success",
                "message": "Client user added successfully.",
                "data": serializer.data,
            },
            status=status.HTTP_201_CREATED,
        )

    def patch(self, request, **kwargs):
        return self._update_delete_client_user(
            request, kwargs.get("client_user_id"), partial=True
        )

    def delete(self, request, **kwargs):
        return self._update_delete_client_user(request, kwargs.get("client_user_id"))

    def _update_delete_client_user(self, request, client_user_id, partial=False):
        if not client_user_id:
            return Response(
                {
                    "status": "failed",
                    "message": "Invalid client_user_id in URL.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        client_user_obj = (
            ClientUser.objects.filter(
                organization=request.user.clientuser.organization, pk=client_user_id
            )
            .select_related("user")
            .first()
        )

        if not client_user_obj:
            return Response(
                {"status": "failed", "message": "Client user not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        self.check_object_permissions(request, client_user_obj)

        with transaction.atomic():
            if partial:
                serializer = self.serializer_class(
                    client_user_obj, data=request.data, partial=partial
                )
                serializer.is_valid(raise_exception=True)
                serializer.save()
                message = "Client user updated successfully."
            else:
                client_user_obj.archived = True
                client_user_obj.user.is_active = False
                client_user_obj.user.email = f"{client_user_obj.user.email}.deleted.{client_user_obj.user.id}-{client_user_obj.organization}"
                client_user_obj.user.phone = f"{client_user_obj.user.phone}.deleted.{client_user_obj.user.id}-{client_user_obj.organization}"
                client_user_obj.user.save()
                client_user_obj.save()
                client_user_obj.jobs.clear()
                if client_user_obj.hiringmanager.exists():
                    transaction.set_rollback(True)
                    return Response(
                        {
                            "status": "failed",
                            "message": "User cannot be deleted because they are assigned to multiple hiring manager roles. Please reassign those jobs before attempting deletion.",
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                message = "Client user successfully deleted."

            response_data = {"status": "success", "message": message}
            if partial:
                response_data["data"] = serializer.data

        return Response(
            response_data,
            status=status.HTTP_200_OK if partial else status.HTTP_204_NO_CONTENT,
        )

    def finalize_response(self, request, response, *args, **kwargs):
        if response.data.get("errors"):
            response.data["status"] = "failed"
            response.data["message"] = response.data.get("message", "Invalid data")
            errors = response.data.pop("errors")
            response.data["errors"] = errors
        return super().finalize_response(request, response, *args, **kwargs)


@extend_schema(tags=["Client"])
class ClientInvitationActivateView(APIView):

    def patch(self, request, uid):
        try:
            decoded_data = force_str(urlsafe_base64_decode(uid))
            inviter_email, invitee_email = [
                item.split(":")[1] for item in decoded_data.split(";")
            ]

            client_user = ClientUser.objects.filter(
                invited_by__email=inviter_email, user__email=invitee_email
            ).first()

            if not client_user:
                return Response(
                    {"status": "failed", "message": "Invalid user"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if (
                datetime.now().timestamp()
                > (client_user.created_at + timedelta(days=2)).timestamp()
            ):
                return Response(
                    {"status": "failed", "message": "Invitation expired"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if client_user.status == "ACT":
                return Response(
                    {"status": "failed", "message": "User is already activated."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            client_user.status = "ACT"
            client_user.save()

            return Response(
                {"status": "success", "message": "User activated successfully."},
                status=status.HTTP_200_OK,
            )
        except Exception:
            return Response(
                {"status": "failed", "message": "Invalid UID"},
                status=status.HTTP_400_BAD_REQUEST,
            )


@extend_schema(tags=["Client"])
class JobView(APIView, LimitOffsetPagination):
    serializer_class = JobSerializer
    permission_classes = [IsAuthenticated, HasRole, UserRoleDeleteUpdateClientData]
    roles_mapping = {
        "GET": [
            Role.CLIENT_ADMIN,
            Role.CLIENT_OWNER,
            Role.CLIENT_USER,
            Role.ADMIN,
            Role.SUPER_ADMIN,
            Role.MODERATOR,
            Role.AGENCY,
        ],
        "POST": [Role.CLIENT_ADMIN, Role.CLIENT_OWNER],
        "PATCH": [Role.CLIENT_ADMIN, Role.CLIENT_OWNER, Role.CLIENT_USER],
        "DELETE": [Role.CLIENT_ADMIN, Role.CLIENT_OWNER, Role.CLIENT_USER],
    }

    def post(self, request):
        serializer = self.serializer_class(
            data=request.data, context={"org": request.user.clientuser.organization}
        )
        if serializer.is_valid():
            serializer.save()
            return Response(
                {
                    "status": "success",
                    "message": "Job created successfully.",
                    "data": serializer.data,
                },
                status=status.HTTP_201_CREATED,
            )
        custom_error = serializer.errors.pop("errors", None)
        return Response(
            {
                "status": "failed",
                "message": "Invalid data.",
                "errors": serializer.errors if not custom_error else custom_error,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    def get(self, request, **kwargs):
        job_id = kwargs.get("job_id")
        active_status = request.query_params.get("status", "active")
        domain_designation_ids = request.query_params.get("job_ids")
        org_id = request.query_params.get(
            "organization_id"
        )  # pass this to get details of for particular client - used in internal engagement section to view details

        try:
            request.user.clientuser
        except User.clientuser.RelatedObjectDoesNotExist:
            if not org_id:
                return Response(
                    {
                        "status": "failed",
                        "message": "Please pass organization_id in params.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            domain_designation_ids = (
                [int(i) for i in domain_designation_ids.split(",")]
                if domain_designation_ids
                else []
            )
        except ValueError:
            return Response(
                {
                    "status": "failed",
                    "message": "Invalid job_ids in query params. It should be comma seperated integer values.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        recruiter_ids = request.query_params.get("recruiter_ids")
        try:
            recruiter_ids = (
                [int(i) for i in recruiter_ids.split(",")] if recruiter_ids else []
            )
        except ValueError:
            return Response(
                {
                    "status": "failed",
                    "message": "Invalid recruiter_ids in query params. It should be comma seperated integer values.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        hiring_manager_ids = request.query_params.get("hiring_manager_ids")
        try:
            hiring_manager_ids = (
                [int(i) for i in hiring_manager_ids.split(",")]
                if hiring_manager_ids
                else []
            )
        except ValueError:
            return Response(
                {
                    "status": "failed",
                    "message": "Invalid hiring_manager_ids in query params. It should be comma seperated integer values.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        post_job_date = request.query_params.get("post_job_date")

        jobs = (
            Job.objects.filter(
                hiring_manager__organization=org_id
                or request.user.clientuser.organization,
            )
            .prefetch_related("clients", "candidate")
            .order_by("-id")
        )

        if active_status == "archive":
            jobs = jobs.exclude(
                Q(reason_for_archived__isnull=True) | Q(reason_for_archived="")
            )
        else:
            jobs = jobs.filter(
                Q(reason_for_archived__isnull=True) | Q(reason_for_archived="")
            )

        if (
            request.user.role in [Role.CLIENT_USER, Role.AGENCY]
            and request.user.clientuser.accessibility == "AGJ"
        ):
            jobs = jobs.filter(clients=request.user.clientuser)

        if domain_designation_ids:
            jobs = jobs.filter(
                name__in=DesignationDomain.objects.filter(
                    pk__in=domain_designation_ids
                ).values_list("name", flat=True)
            )

        if recruiter_ids:
            jobs = jobs.filter(clients__in=recruiter_ids)

        if hiring_manager_ids:
            jobs = jobs.filter(hiring_manager__in=hiring_manager_ids)

        if post_job_date:
            try:
                post_job_date = (
                    datetime.strptime(post_job_date, "%d/%m/%Y")
                    .date()
                    .strftime("%Y-%m-%d")
                )
                jobs = jobs.filter(created_at__date=post_job_date)
            except ValueError:
                return Response(
                    {
                        "status": "failed",
                        "message": "Invalid post_job_date in query params. It should be in DD/MM/YYYY format.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if job_id:
            job = jobs.filter(pk=job_id).first()
            if not job:
                return Response(
                    {
                        "status": "failed",
                        "message": "Job not found.",
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )
            serializer = self.serializer_class(job)
            return Response(
                {
                    "status": "success",
                    "message": "Job retrieved successfully.",
                    "data": serializer.data,
                },
                status=status.HTTP_200_OK,
            )

        paginated_jobs = self.paginate_queryset(jobs, request)
        serializer = self.serializer_class(paginated_jobs, many=True)
        response_data = self.get_paginated_response(serializer.data)

        return Response(
            {
                "status": "success",
                "message": "Jobs retrieved successfully.",
                **response_data.data,
            },
            status=status.HTTP_200_OK,
        )

    def patch(self, request, **kwargs):
        job_id = kwargs.get("job_id")
        if not job_id:
            return Response(
                {
                    "status": "failed",
                    "message": "Invalid job_id in url.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            job = Job.objects.get(
                hiring_manager__organization_id=request.user.clientuser.organization_id,
                pk=job_id,
            )
            self.check_object_permissions(request, job)
        except Job.DoesNotExist:
            return Response(
                {
                    "status": "failed",
                    "message": "Job not found.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = self.serializer_class(
            job,
            data=request.data,
            partial=True,
            context={"org": request.user.clientuser.organization},
        )
        if serializer.is_valid():
            serializer.save()
            return Response(
                {
                    "status": "success",
                    "message": "Job updated successfully.",
                    "data": serializer.data,
                },
                status=status.HTTP_200_OK,
            )

        custom_error = serializer.errors.pop("errors", None)
        return Response(
            {
                "status": "failed",
                "message": "Invalid data.",
                "errors": serializer.errors if not custom_error else custom_error,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    def delete(self, request, **kwargs):
        job_id = kwargs.get("job_id")
        if not job_id:
            return Response(
                {
                    "status": "failed",
                    "message": "Invalid job_id in url.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            job = Job.objects.get(
                hiring_manager__organization_id=request.user.clientuser.organization_id,
                pk=job_id,
            )
            self.check_object_permissions(request, job)
        except Job.DoesNotExist:
            return Response(
                {
                    "status": "failed",
                    "message": "Job not found.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        job.archived = True
        job.save()

        return Response(
            {
                "status": "success",
                "message": "Job deleted successfully.",
            },
            status=status.HTTP_204_NO_CONTENT,
        )


@extend_schema(tags=["Client"])
class ResumeParserView(APIView):
    permission_classes = [
        IsAuthenticated,
        IsClientAdmin | IsClientUser | IsClientOwner | IsAgency | IsSuperAdmin,
    ]

    def post(self, request):
        resume_files = request.FILES.getlist("resume")

        if not resume_files:
            return Response(
                {
                    "status": "failed",
                    "message": "Invalid request.",
                    "error": {
                        "resume": [
                            "This field is required. Up to 15 PDF or DOCX resumes are supported."
                        ]
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if len(resume_files) > 15:
            return Response(
                {"status": "failed", "message": "You can upload up to 15 files only."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        errors = {}
        for f in resume_files:
            err = validate_attachment("resume", f, ["pdf", "docx", "doc"], 5)
            if err:
                errors[f.name] = err
        if errors:
            return Response(
                {
                    "status": "failed",
                    "message": "Some files are invalid.",
                    "error": errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        temp_dir = tempfile.mkdtemp()
        temp_paths = []

        try:
            for f in resume_files:
                temp_path = os.path.join(temp_dir, f.name)
                with open(temp_path, "wb") as temp_file:
                    for chunk in f.chunks():
                        temp_file.write(chunk)
                temp_paths.append(temp_path)

            parsed_data = process_resumes(temp_paths)
            return Response(
                {
                    "status": "success",
                    "message": "Resumes parsed successfully.",
                    "data": parsed_data,
                },
                status=status.HTTP_200_OK,
            )

        finally:
            for path in temp_paths:
                try:
                    os.remove(path)
                except Exception:
                    pass
            os.rmdir(temp_dir)


@extend_schema(tags=["Client"])
class CandidateView(APIView, LimitOffsetPagination):
    serializer_class = CandidateSerializer
    permission_classes = [
        IsAuthenticated,
        IsClientAdmin | IsClientUser | IsClientOwner | IsAgency,
        UserRoleDeleteUpdateClientData,
    ]

    def get(self, request, **kwargs):
        candidate_id = kwargs.get("candidate_id")
        domain_designation_id = request.query_params.get("job_id")
        status_ = request.query_params.get("status")
        search_term = request.query_params.get("q")
        specialization = request.query_params.get("specialization")

        if (
            search_term
            and search_term.isdigit()
            and len(search_term) > 2
            and not search_term.startswith("+91")
        ):
            search_term = "+91" + search_term

        if (
            status_
            and status_
            not in dict(
                Candidate.STATUS_CHOICES + Candidate.FINAL_SELECTION_STATUS_CHOICES
            ).keys()
        ):
            return Response(
                {"status": "failed", "message": "Invalid Status."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if domain_designation_id and not domain_designation_id.isdigit():
            return Response(
                {
                    "status": "failed",
                    "message": "Invalid domain_designation_id format in query_params",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if domain_designation_id and not (
            domain_designation := DesignationDomain.objects.filter(
                pk=domain_designation_id
            ).first()
        ):
            return Response(
                {
                    "status": "failed",
                    "message": "Invalid domain_designation_id in query_params",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        candidates = (
            Candidate.objects.filter(organization=request.user.clientuser.organization)
            .select_related("designation")
            .order_by("-id")
        )

        if (
            request.user.role in [Role.CLIENT_USER, Role.AGENCY]
            and request.user.clientuser.accessibility == "AGJ"
        ):
            candidates = candidates.filter(designation__clients=request.user.clientuser)

        total_candidates = candidates.count()
        scheduled = candidates.filter(status__in=["SCH", "CSCH"]).count()
        inprocess = candidates.filter(status="NSCH").count()
        recommended = candidates.filter(Q(status="REC") | Q(status="HREC")).count()
        rejected = candidates.filter(Q(status="SNREC") | Q(status="NREC")).count()

        if domain_designation_id and domain_designation:
            designation_name = domain_designation.name
            candidates = candidates.filter(designation__name=designation_name)

        if status_:
            if status_ == "SCH":
                candidates = candidates.filter(
                    Q(status__in=["SCH", "CSCH"]) | Q(final_selection_status=status_)
                )
            else:
                candidates = candidates.filter(
                    Q(status=status_) | Q(final_selection_status=status_)
                )

        if specialization:
            candidates = candidates.filter(specialization=specialization)

        if search_term:
            candidates = candidates.filter(
                Q(name__icontains=search_term)
                | Q(email__iexact=search_term)
                | Q(phone__startswith=search_term)
            )

        if candidate_id:
            candidate = candidates.filter(pk=candidate_id).first()
            if not candidate:
                return Response(
                    {"status": "failed", "message": "Candidate not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            serializer = self.serializer_class(candidate)
            return Response(
                {
                    "status": "success",
                    "message": "Candidate retrieved successfully.",
                    "data": serializer.data,
                },
                status=status.HTTP_200_OK,
            )

        paginated_candidates = self.paginate_queryset(candidates, request)
        serializer = self.serializer_class(paginated_candidates, many=True)
        paginated_response = self.get_paginated_response(serializer.data)
        response_data = {
            "status": "success",
            "message": "Candidates retrieved successfully.",
            "total_candidates": total_candidates,
            "scheduled": scheduled,
            "inprocess": inprocess,
            "recommended": recommended,
            "rejected": rejected,
            **paginated_response.data,
        }
        return Response(response_data, status=status.HTTP_200_OK)

    def post(self, request, **kwargs):
        serializer = self.serializer_class(
            data=request.data, context={"request": request}
        )
        if serializer.is_valid():
            serializer.save(
                organization=request.user.clientuser.organization,
                designation_id=serializer.validated_data.pop("job_id"),
                added_by=request.user.clientuser,
            )
            return Response(
                {
                    "status": "success",
                    "message": "Candidate stored successfully",
                    "data": serializer.data,
                },
                status=status.HTTP_201_CREATED,
            )

        custom_error = serializer.errors.pop("errors", None)
        return Response(
            {
                "status": "failed",
                "message": "Invalid data.",
                "errors": serializer.errors if not custom_error else custom_error,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    def patch(self, request, **kwargs):
        candidate_id = kwargs.get("candidate_id")
        candidate_instance = self.get_candidate_instance(request, candidate_id)
        if isinstance(candidate_instance, Response):
            return candidate_instance
        serializer = self.serializer_class(
            candidate_instance, request.data, partial=True, context={"request": request}
        )
        if serializer.is_valid():
            serializer.save()
            return Response(
                {
                    "status": "success",
                    "message": "Successfully updated candidate profile",
                    "data": serializer.data,
                },
                status=status.HTTP_200_OK,
            )
        custom_errors = serializer.errors.pop("errors", None)
        return Response(
            {
                "status": "failed",
                "message": "Failed to update candidate profile",
                "errors": custom_errors if custom_errors else serializer.errors,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    def delete(self, request, **kwargs):
        candidate_id = kwargs.get("candidate_id")
        reason_for_dropping = request.data.get("reason")
        if (
            reason_for_dropping
            not in dict(Candidate.REASON_FOR_DROPPING_CHOICES).keys()
        ):
            return Response(
                {
                    "status": "failed",
                    "message": "Invalid reason for dropping. Please choose from the following options: {}".format(
                        ", ".join(
                            [
                                "{} ({})".format(key, value)
                                for key, value in dict(
                                    Candidate.REASON_FOR_DROPPING_CHOICES
                                ).items()
                            ]
                        )
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        candidate_instance = self.get_candidate_instance(request, candidate_id)
        if isinstance(candidate_instance, Response):
            return candidate_instance

        if (
            candidate_instance.status == ["SNREC", "NREC", "NJ"]
            and reason_for_dropping != "RJD"
        ):
            return Response({"status": "failed", "message": "Invalid reason."})
        elif candidate_instance.status == ["HREC", "REC", "CSCH"]:
            return Response(
                {
                    "status": "failed",
                    "message": "Candidate cannot be dropped because they are already scheduled or processed.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if reason_for_dropping:
            candidate_instance.reason_for_dropping = reason_for_dropping

        candidate_instance.archived = True
        candidate_instance.save()
        return Response(
            {"status": "success", "message": "Candidate dropped successfully"},
            status=status.HTTP_204_NO_CONTENT,
        )

    def get_candidate_instance(self, request, candidate_id):
        try:
            candidate_instance = Candidate.objects.get(
                organization=request.user.clientuser.organization, pk=candidate_id
            )
            self.check_object_permissions(request, candidate_instance)
        except Candidate.DoesNotExist:
            return Response(
                {"status": "failed", "message": "Candidate not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return candidate_instance


@extend_schema(tags=["Client"])
class PotentialInterviewerAvailabilityForCandidateView(APIView):
    serializer_class = None
    permission_classes = [
        IsAuthenticated,
        IsClientAdmin | IsClientOwner | IsClientUser | IsAgency,
    ]

    def get(self, request):
        date = request.query_params.get("date")
        time = request.query_params.get("time")
        specialization = request.query_params.get("specialization")
        experience = request.query_params.get("experience_year")
        company = request.query_params.get("company")
        designation_id = request.query_params.get("designation_id")

        required_fields = {
            "date": date,
            # "time": time,
            "designation_id": designation_id,
            "experience_year": experience,
            "specialization": specialization,
            "company": company,
        }
        missing_fields = [
            field for field, value in required_fields.items() if not value
        ]
        if missing_fields:
            return Response(
                {
                    "status": "failed",
                    "message": f"{', '.join(missing_fields)} are required in query params.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            formatted_date = datetime.strptime(date, "%d/%m/%Y").date()
            if formatted_date < datetime.today().date():
                return Response(
                    {"status": "failed", "message": "Invalid date"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if time:
                formatted__start_time = datetime.strptime(time, "%H:%M").time()
                if (
                    formatted_date == datetime.today().date()
                    and formatted__start_time < datetime.now().time()
                ):
                    return Response(
                        {"status": "failed", "message": "Invalid time"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                end_time = (
                    datetime.strptime(time, "%H:%M") + timedelta(hours=1)
                ).time()
        except ValueError:
            return Response(
                {
                    "status": "failed",
                    "message": "Invalid date or time format. Use DD/MM/YYYY and HH:MM",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            experience = int(experience) if experience is not None else 0
        except ValueError:
            return Response(
                {
                    "status": "failed",
                    "message": "Invalid experience format. It should be a valid integer",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if specialization not in dict(Candidate.SPECIALIZATION_CHOICES).keys():
            return Response(
                {
                    "status": "failed",
                    "message": "Invalid specialization. Please choose from the following options: {}".format(
                        ", ".join(
                            "{} ({})".format(key, value)
                            for key, value in dict(
                                Candidate.SPECIALIZATION_CHOICES
                            ).items()
                        )
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            job = Job.objects.get(
                pk=designation_id,
                hiring_manager__organization=request.user.clientuser.organization,
            )
        except Job.DoesNotExist:
            return Response(
                {"status": "failed", "message": "Job not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        skills = job.mandatory_skills or []
        if not skills:
            return Response(
                {
                    "status": "failed",
                    "message": "No mandatory skills found for this job.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        query = Q()
        for skill in skills:
            query |= Q(interviewer__skills__icontains=f'"{skill}"')

        client_level = request.user.clientuser.organization.internal_client.client_level
        interviewer_level = (
            list(range(client_level - 1, client_level + 1))
            if client_level in [2, 3]
            else [client_level]
        )

        interviewer_availability = InterviewerAvailability.objects.select_related(
            "interviewer"
        ).filter(
            date=formatted_date,
            interviewer__assigned_domains__name=job.name,
            interviewer__strength=specialization,
            interviewer__total_experience_years__gte=experience + 2,
            interviewer__interviewer_level__in=interviewer_level,
            booked_by__isnull=True,
        )

        if time:
            interviewer_availability = interviewer_availability.filter(
                start_time__lte=formatted__start_time, end_time__gte=end_time
            )

        interviewer_availability = (
            interviewer_availability.filter(query)
            .exclude(interviewer__current_company__iexact=company)
            .values("id", "date", "start_time", "end_time")
        )

        if not interviewer_availability:
            return Response(
                {"status": "failed", "message": "No available slots on that date."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                "status": "success",
                "message": "Available slots retrieved successfully.",
                "data": list(interviewer_availability),
            },
            status=status.HTTP_200_OK,
        )


@extend_schema(tags=["Client"])
class EngagementTemplateView(APIView, LimitOffsetPagination):
    permission_classes = [IsAuthenticated, IsClientOwner | IsClientAdmin | IsClientUser]
    serializer_class = EngagementTemplateSerializer

    def get(self, request, **kwrags):
        engagement_template_qs = EngagementTemplates.objects.filter(
            organization=request.user.clientuser.organization
        )
        paginated_queryset = self.paginate_queryset(engagement_template_qs, request)
        serializer = self.serializer_class(paginated_queryset, many=True)
        paginated_response = self.get_paginated_response(serializer.data)
        return Response(
            {
                "status": "success",
                "message": "Successfully retrieved templates",
                **paginated_response.data,
            }
        )

    def post(self, request, **kwargs):
        serializer = self.serializer_class(
            data=request.data, context={"attachment": request.FILES.get("attachment")}
        )
        if serializer.is_valid():
            serializer.save(organization=request.user.clientuser.organization)
            return Response(
                {
                    "status": "success",
                    "message": "Successfully created template",
                    "data": serializer.data,
                },
                status=status.HTTP_201_CREATED,
            )
        custom_errors = serializer.errors.pop("errors", None)
        return Response(
            {
                "status": "failed",
                "message": "Invalid data",
                "errors": custom_errors if custom_errors else serializer.errors,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    def patch(self, request, pk):
        try:
            engagement_template = EngagementTemplates.objects.get(
                pk=pk, organization=request.user.clientuser.organization
            )
        except EngagementTemplates.DoesNotExist:
            return Response(
                {"status": "failed", "message": "Template not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = self.serializer_class(
            engagement_template, data=request.data, partial=True
        )
        if serializer.is_valid():
            serializer.save()
            return Response(
                {
                    "status": "success",
                    "message": "Successfully updated template",
                    "data": serializer.data,
                },
                status=status.HTTP_200_OK,
            )
        return Response(
            {
                "status": "failed",
                "message": "Invalid data",
                "errors": serializer.errors,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    def delete(self, request, pk):
        try:
            engagement_template = EngagementTemplates.objects.get(
                pk=pk, organization=request.user.clientuser.organization
            )
        except EngagementTemplates.DoesNotExist:
            return Response(
                {"status": "failed", "message": "Template not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        engagement_template.archived = True
        engagement_template.save(update_fields=["archived"])
        return Response(
            {"status": "success", "message": "Successfully deleted template"},
            status=status.HTTP_204_NO_CONTENT,
        )


@extend_schema(tags=["Client"])
class EngagementView(APIView, LimitOffsetPagination):
    serializer_class = EngagementSerializer
    permission_classes = [IsAuthenticated, HasRole]
    roles_mapping = {
        "GET": [
            Role.CLIENT_ADMIN,
            Role.CLIENT_OWNER,
            Role.CLIENT_USER,
            Role.ADMIN,
            Role.SUPER_ADMIN,
            Role.MODERATOR,
        ],
        "POST": [Role.CLIENT_ADMIN, Role.CLIENT_OWNER, Role.CLIENT_USER],
        "PATCH": [Role.CLIENT_ADMIN, Role.CLIENT_OWNER, Role.CLIENT_USER],
    }

    def post(self, request, **kwargs):
        engagement_id = kwargs.get("engagement_id")
        if engagement_id:
            return Response(
                {"status": "failed", "message": "Invalid request."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = self.serializer_class(
            data=request.data, context={"request": request}
        )
        if serializer.is_valid():
            candidate = serializer.validated_data.get("candidate")
            serializer.save(organization=request.user.clientuser.organization)
            if candidate:
                candidate.is_engagement_pushed = True
                candidate.save()
            return Response(
                {
                    "status": "success",
                    "message": "Successfully created engagement",
                    "data": serializer.data,
                },
                status=status.HTTP_201_CREATED,
            )
        custom_errors = serializer.errors.pop("errors", None)
        return Response(
            {
                "status": "failed",
                "message": "Invalid data",
                "errors": custom_errors if custom_errors else serializer.errors,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    def get(self, request, **kwargs):
        engagement_id = kwargs.get("engagement_id")
        if engagement_id:
            return Response(
                {"status": "failed", "message": "Invalid request."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        query_params = request.query_params
        domain_designation_ids = query_params.get("job_ids")
        specialization = query_params.get("specializations")
        notice_period = query_params.get("nps")
        status_ = query_params.get("status")
        search_filter = query_params.get("q")
        org_id = query_params.get(
            "organization_id"
        )  # pass this to get details of for particular client - used in internal engagement section to view details

        try:
            org = request.user.clientuser.organization
        except User.clientuser.RelatedObjectDoesNotExist:
            if not org_id:
                return Response(
                    {
                        "status": "failed",
                        "message": "Please pass organization_id in params.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if org_id:
            org = Organization.objects.filter(id=org_id).first()
            if not org:
                return Response(
                    {"status": "failed", "message": "Invalid organization_id"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if status_:
            invalid_statuses = [
                s
                for s in status_.split(",")
                if s not in dict(Engagement.STATUS_CHOICE).keys()
            ]
            if invalid_statuses:
                return Response(
                    {
                        "status": "failed",
                        "message": f"Invalid Status(es): {', '.join(invalid_statuses)}.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if (
            search_filter
            and search_filter.isdigit()
            and len(search_filter) > 2
            and not search_filter.startswith("+91")
        ):
            search_filter = "+91" + search_filter

        filters = {
            "organization_id": org_id or request.user.clientuser.organization_id,
            "status__in": ["YTJ", "DBT", "OHD"],
        }
        if domain_designation_ids:
            ids = [int(id) for id in domain_designation_ids.split(",") if id.isdigit()]
            filters["candidate__designation__name__in"] = (
                DesignationDomain.objects.filter(pk__in=ids).values_list(
                    "name", flat=True
                )
            )
        if specialization:
            filters["candidate__specialization__in"] = specialization.split(",")
        if notice_period:
            filters["notice_period__in"] = notice_period.split(",")
        if status_:
            filters["status__in"] = status_.split(",")

        engagement_summary = Engagement.objects.filter(
            organization=org or request.user.clientuser.organization
        ).aggregate(
            total_candidates=Count("id"),
            joined=Count("id", filter=Q(status="JND")),
            declined=Count("id", filter=Q(status="DCL")),
            pending=Count("id", filter=Q(status="YTJ")),
        )

        engagements = (
            Engagement.objects.select_related("candidate")
            .prefetch_related("engagementoperations")
            .filter(**filters)
        )

        if search_filter:
            engagements = engagements.filter(
                Q(candidate_name__icontains=search_filter)
                | Q(candidate__name__icontains=search_filter)
                | Q(candidate_email__iexact=search_filter)
                | Q(candidate__email__iexact=search_filter)
                | Q(candidate_phone__startswith=search_filter)
                | Q(candidate__phone__startswith=search_filter)
                | Q(job__icontains=search_filter)
            )

        paginated_engagements = self.paginate_queryset(engagements, request)
        serializer = self.serializer_class(paginated_engagements, many=True)
        paginated_response = self.get_paginated_response(serializer.data)

        return Response(
            {
                "status": "success",
                "message": "Successfully retrieved engagements",
                **engagement_summary,
                **paginated_response.data,
            },
            status=status.HTTP_200_OK,
        )

    def patch(self, request, **kwargs):
        engagement_id = kwargs.get("engagement_id")
        if not engagement_id:
            return Response(
                {"status": "failed", "message": "Engagement id is required in url"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        engagement = Engagement.objects.filter(
            organization=request.user.clientuser.organization, pk=engagement_id
        ).first()

        if not engagement:
            return Response(
                {"status": "failed", "message": "Invalid engagement id"},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = EngagementUpdateStatusSerializer(
            engagement, data=request.data, partial=True, context={"request": request}
        )

        if serializer.is_valid():
            serializer.save()
            return Response(
                {"status": "success", "message": "Engagement updated successfully"},
                status=status.HTTP_200_OK,
            )

        custom_errors = serializer.errors.pop("errors", None)
        return Response(
            {
                "status": "failed",
                "message": "Invalid data.",
                "errors": custom_errors if custom_errors else serializer.errors,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )


@extend_schema(tags=["Client"])
class EngagementOperationView(APIView, LimitOffsetPagination):
    serializer_class = EngagementOperationSerializer
    permission_classes = [IsAuthenticated, IsClientAdmin | IsClientOwner | IsClientUser]

    def post(self, request):
        serializer = self.serializer_class(
            data=request.data, context={"request": request}
        )
        if serializer.is_valid():
            serializer.save()
            return Response(
                {
                    "status": "success",
                    "message": "Engagement operation initiated successfully",
                    "data": serializer.data,
                },
                status=status.HTTP_201_CREATED,
            )

        custom_errors = serializer.errors.pop("errors", None)
        return Response(
            {
                "status": "failed",
                "message": "Failed to initiate the engagement operation",
                "errors": custom_errors if custom_errors else serializer.errors,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    """    --> keep it for future reference
    def get(self, request):
        organization = request.user.clientuser.organization
        engagement_operation = EngagementOperation.objects.filter(
            engagement__organization=organization
        )
        paginated_engagements = self.paginate_queryset(engagement_operation, request)
        serializer = self.serializer_class(paginated_engagements, many=True)
        paginated_response = self.get_paginated_response(serializer.data)
        return Response(
            {
                "status": "success",
                "message": "Successfully retrieved engagements",
                **paginated_response.data,
            },
            status=status.HTTP_200_OK,
        )
    """


@extend_schema(tags=["Client"])
class EngagementOperationUpdateView(APIView):
    permission_classes = [IsAuthenticated, IsClientAdmin | IsClientOwner | IsClientUser]

    def put(self, request, engagement_id):
        with transaction.atomic():
            engagement_operations = EngagementOperation.objects.filter(
                engagement__organization=request.user.clientuser.organization,
                engagement_id=engagement_id,
            )

            if "template_data" not in request.data or not isinstance(
                request.data["template_data"], list
            ):
                return Response(
                    {
                        "status": "failed",
                        "message": "template_data is required",
                        "errors": {
                            "template_data": [
                                "This field must be a non-empty list of dictionaries with keys 'template_id' and 'date'.",
                                "Expected format: [{'template_id': <int>, 'operation_id': <int>(optional), 'operation_complete_status': <string>(optional), 'week': <int>(optional), 'date': '<dd/mm/yyyy hh:mm:ss>'}]",
                            ]
                        },
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            template_data = request.data.get("template_data")
            for entry in template_data:
                if (
                    not isinstance(entry, dict)
                    or ("template_id" not in entry and "operation_id" not in entry)
                    or ("operation_id" not in entry and "week" not in entry)
                    or "date" not in entry
                ):
                    return Response(
                        {
                            "status": "failed",
                            "message": "Invalid template data",
                            "errors": {
                                "template_data": [
                                    "Each item must match the following schema:",
                                    "Expected format: {'template_id': <int>, 'operation_id': <int>(optional), 'operation_complete_status':<string>(optional), 'week': <int>(optional), 'date': '<dd/mm/yyyy hh:mm:ss>'}",
                                ]
                            },
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            for template in template_data:
                try:
                    datetime.strptime(template["date"], "%d/%m/%Y %H:%M:%S")
                except ValueError:
                    return Response(
                        {
                            "status": "failed",
                            "message": "Invalid date format",
                            "errors": {
                                "template_data": [
                                    "Each item must have a 'date' in this format: '%d/%m/%Y %H:%M:%S'",
                                ]
                            },
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            incoming_template_ids = [
                template["template_id"] for template in template_data
            ]

            valid_template_ids = set(
                EngagementTemplates.objects.filter(
                    organization=request.user.clientuser.organization,
                    pk__in=incoming_template_ids,
                ).values_list("id", flat=True)
            )
            invalid_template_ids = set(incoming_template_ids) - valid_template_ids

            if invalid_template_ids:
                return Response(
                    {
                        "status": "failed",
                        "message": "Invalid template IDs",
                        "errors": {
                            "template_data": [
                                "Invalid template_id: {}".format(
                                    ", ".join(map(str, invalid_template_ids))
                                )
                            ]
                        },
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # validation of template ids uniqueness
            if len(incoming_template_ids) != len(set(incoming_template_ids)):
                return Response(
                    {
                        "status": "failed",
                        "message": "Template IDs must be unique",
                        "errors": {
                            "template_data": [
                                "Template IDs must be unique, but got duplicates"
                            ]
                        },
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            operation_ids = {
                entry.get("operation_id")
                for entry in template_data
                if "operation_id" in entry
            }
            valid_operation_ids = set(
                engagement_operations.values_list("id", flat=True)
            )
            invalid_operation_ids = operation_ids - valid_operation_ids

            if invalid_operation_ids:
                return Response(
                    {
                        "status": "failed",
                        "message": "Invalid operation IDs",
                        "errors": {
                            "template_data": [
                                f"operation_id {', '.join(map(str, invalid_operation_ids))} do not exist."
                            ]
                        },
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Prevent updating operations that are already successful
            locked_operations = engagement_operations.filter(
                pk__in=operation_ids, delivery_status="SUC"
            ).values_list("id", flat=True)

            # retrieving edge case validation delete operation_ids
            validation_operation_ids_after_success_operation = set(
                engagement_operations.filter(~Q(delivery_status="SUC")).values_list(
                    "id", flat=True
                )
            )
            delete_operation_ids = (
                validation_operation_ids_after_success_operation - operation_ids
            )

            # validating dates of only allowed operations
            invalid_dates = [
                template["date"]
                for template in template_data
                if (
                    "operation_id" not in template
                    or template["operation_id"] not in locked_operations
                )
                and datetime.strptime(
                    template["date"],
                    "%d/%m/%Y %H:%M:%S",
                )
                < datetime.strptime(
                    datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                    "%d/%m/%Y %H:%M:%S",
                )
            ]

            if invalid_dates:
                return Response(
                    {
                        "status": "failed",
                        "message": "Invalid dates in template data",
                        "errors": {
                            "template_data": [
                                f"Dates in the past are not allowed: {', '.join(invalid_dates)}"
                            ]
                        },
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            for template in template_data:
                template["date"] = datetime.strptime(
                    template["date"], "%d/%m/%Y %H:%M:%S"
                )

            # Dictionary mapping operation_id -> data
            operation_data_map = {
                entry["operation_id"]: entry
                for entry in template_data
                if "operation_id" in entry
                and entry["operation_id"] not in locked_operations
            }

            locked_operation_data_map = {
                entry["operation_id"]: entry
                for entry in template_data
                if "operation_id" in entry
                and entry["operation_id"] in locked_operations
            }

            # New operations (ones without operation_id)
            new_operations = [
                entry for entry in template_data if "operation_id" not in entry
            ]

            # Get engagement details
            engagement = Engagement.objects.filter(pk=engagement_id).first()
            if not engagement:
                return Response(
                    {"status": "failed", "message": "Engagement not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            notice_weeks = int(engagement.notice_period.split("-")[1]) / 7
            max_template_assign = notice_weeks * 2

            # Validate new template assignment limit
            # existing_count = EngagementOperation.objects.filter(
            #     engagement=engagement
            # ).count()
            if len(template_data) > max_template_assign:
                return Response(
                    {
                        "status": "failed",
                        "message": "Max template assignment exceeded",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Validate max templates per week
            week_count_map = {}
            for template in template_data:
                week = template.get("week")
                week_count_map[week] = week_count_map.get(week, 0) + 1
                if week is not None and week_count_map[week] > 2:
                    return Response(
                        {
                            "status": "failed",
                            "message": f"Week {week} has exceeded max templates (2).",
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            rescheduled_operations = []
            locked_update_template_operation = []
            for operation in engagement_operations.filter(pk__in=operation_ids):

                locked_template_entry = locked_operation_data_map.get(operation.id)
                if locked_template_entry:
                    operation.operation_complete_status = locked_template_entry.get(
                        "operation_complete_status", "PED"
                    )
                    locked_update_template_operation.append(operation)

                template_entry = operation_data_map.get(operation.id)
                if template_entry:
                    if (
                        timezone.localtime(operation.date).strftime("%d/%m/%Y %H:%M:%S")
                        != template_entry["date"].strftime("%d/%m/%Y %H:%M:%S")
                        or operation.template_id != template_entry["template_id"]
                    ):
                        # **Revoke old task if it exists**
                        if operation.task_id:
                            result = AsyncResult(str(operation.task_id))
                            print(result.state)
                            if result.state in [
                                "PENDING",
                                "RECEIVED",
                                "STARTED",
                                "QUEUED",
                            ]:
                                result.revoke(terminate=True, signal="SIGTERM")

                        if result.state in ["FAILURE", "RETRY"]:
                            result.forget()

                        # **Schedule a new task**
                        new_eta = timezone.make_aware(template_entry["date"])
                        new_task = send_schedule_engagement_email.s(operation.id).set(
                            eta=new_eta
                        )
                        new_task_result = new_task.apply_async()

                        # **Update task ID with the new one**
                        operation.task_id = new_task_result.id

                    operation.template_id = template_entry["template_id"]
                    operation.date = template_entry["date"]
                    operation.week = template_entry.get("week", operation.week)
                    rescheduled_operations.append(operation)

            # Bulk update modified operations
            if rescheduled_operations:
                EngagementOperation.objects.bulk_update(
                    rescheduled_operations, ["template_id", "date", "week", "task_id"]
                )

            # update the successfull status
            if locked_update_template_operation:
                EngagementOperation.objects.bulk_update(
                    locked_update_template_operation, ["operation_complete_status"]
                )

            # cancel the deleted operation ids task
            delete_scheduled_operations = []
            for delete_operation in EngagementOperation.objects.filter(
                pk__in=delete_operation_ids
            ):
                AsyncResult(delete_operation.task_id).revoke(terminate=True)
                delete_operation.archived = True
                delete_scheduled_operations.append(delete_operation)

            # Delete the cancelled operations
            if delete_scheduled_operations:
                EngagementOperation.objects.bulk_update(
                    delete_scheduled_operations, ["archived"]
                )

            if new_operations:
                # Bulk create new operations
                created_operations = EngagementOperation.objects.bulk_create(
                    [
                        EngagementOperation(
                            engagement=engagement,
                            template_id=entry["template_id"],
                            date=entry["date"],
                            week=entry.get("week"),
                        )
                        for entry in new_operations
                    ],
                )

                if not all(op.id for op in created_operations):
                    created_operations = list(
                        EngagementOperation.objects.filter(
                            engagement__in=[
                                operation.engagement for operation in created_operations
                            ]
                        )
                    )

                task_group = group(
                    send_schedule_engagement_email.s(operation.id).set(
                        eta=operation.date
                    )
                    for operation in created_operations
                )
                result = task_group.apply_async()

                # Assign task IDs in bulk update
                for operation, task in zip(created_operations, result.children):
                    operation.task_id = task.id

                EngagementOperation.objects.bulk_update(created_operations, ["task_id"])

        return Response(
            {
                "status": "success",
                "message": "Engagement operations updated successfully.",
            },
            status=status.HTTP_200_OK,
        )


class EngagementOperationStatusUpdateView(APIView):
    permission_classes = (IsAuthenticated, IsClientAdmin | IsClientOwner | IsClientUser)
    serializer_class = EngagmentOperationStatusUpdateSerializer

    def put(self, request, engagement_operation_id):
        engagement_operation = EngagementOperation.objects.filter(
            template__organization=request.user.clientuser.organization,
            pk=engagement_operation_id,
        ).first()
        if not engagement_operation:
            return Response(
                {"status": "failed", "message": "Engagement operation does not exist"},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = self.serializer_class(engagement_operation, request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(
                {
                    "status": "success",
                    "message": "Engagement operation status updated successfully.",
                },
                status=status.HTTP_200_OK,
            )

        custom_errors = serializer.errors.pop("errors", None)
        return Response(
            {
                "status": "failed",
                "message": "Invalid data",
                "errors": serializer.errors if not custom_errors else custom_errors,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )


class ClientDashboardView(APIView):
    permission_classes = (
        IsAuthenticated,
        IsClientAdmin | IsClientOwner | IsClientUser | IsAgency,
    )
    serializer_class = None

    def get(self, request):
        organization = request.user.clientuser.organization

        all_jobs = Job.objects.filter(hiring_manager__organization=organization)
        candidates = Candidate.objects.filter(organization=organization)
        if (
            request.user.role in [Role.CLIENT_USER, Role.AGENCY]
            and request.user.clientuser.accessibility == "AGJ"
        ):
            all_jobs = all_jobs.filter(clients=request.user.clientuser)
            candidates = candidates.filter(designation__clients=request.user.clientuser)

        # Job role aggregates
        job_role_aggregates = all_jobs.values("name").annotate(
            count=Count(
                "id",
                filter=Q(reason_for_archived__isnull=True) | Q(reason_for_archived=""),
            )
        )

        # Candidate progress aggregates
        candidates = candidates.aggregate(
            total_interviews=Count(
                "id", filter=Q(status__in=["COMPLETED", "HREC", "REC", "NREC", "SNREC"])
            ),
            pending_schedule=Count("id", filter=Q(status="NSCH")),
            selects=Count("id", filter=Q(final_selection_status="SLD")),
            joined=Count("id", filter=Q(engagements__status="JND")),
        )

        # Job aggregation
        job_aggregates = all_jobs.aggregate(
            total_jobs=Count("id", distinct=True),
            total_candidates=Count("candidate"),
            selects=Count(
                "candidate", filter=Q(candidate__final_selection_status="SLD")
            ),
            rejects=Count(
                "candidate", filter=Q(candidate__final_selection_status="RJD")
            ),
        )

        data = {
            "job_role_aggregates": list(job_role_aggregates),
            "candidates": candidates,
            "job_aggregates": job_aggregates,
        }

        return Response(
            {
                "status": "success",
                "message": "Dashboard data fetched successfully.",
                "data": data,
            },
            status=status.HTTP_200_OK,
        )


class FinanceView(APIView, LimitOffsetPagination):
    serializer_class = FinanceSerializer
    permission_classes = [
        IsAuthenticated,
        IsClientOwner | IsSuperAdmin | IsAdmin | IsModerator | IsInterviewer,
    ]

    def get(self, request):
        organization_id = request.query_params.get("organization_id")
        interviewer_id = request.query_params.get("interviewer_id")
        finance_month = request.query_params.get("finance_month", "current_month")

        start_date = request.query_params.get("start_date")
        if start_date:
            try:
                start_date = timezone.make_aware(
                    datetime.strptime(start_date, "%d/%m/%Y")
                )
            except ValueError:
                return Response(
                    {
                        "status": "failed",
                        "message": "Invalid start_date. It should be in %d/%m/%Y format.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        end_date = request.query_params.get("end_date")
        if end_date:
            try:
                end_date = timezone.make_aware(datetime.strptime(end_date, "%d/%m/%Y"))
            except ValueError:
                return Response(
                    {
                        "status": "failed",
                        "message": "Invalid end_date. It should be in %d/%m/%Y format.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if request.user.role not in [Role.CLIENT_OWNER, Role.INTERVIEWER] and not (
            organization_id or interviewer_id
        ):
            return Response(
                {
                    "status": "failed",
                    "message": "Either organization_id or interviewer_id are required.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        url_name = request.resolver_match.url_name
        if (
            (url_name == "client-finance" and request.user.role != Role.CLIENT_OWNER)
            or (
                url_name == "interviewer-finance"
                and request.user.role != Role.INTERVIEWER
            )
            or url_name == "internal-finance"
            and not (organization_id or interviewer_id)
        ):
            return Response(
                {"status": "failed", "message": "Invalid Request"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        today = dt.date.today()
        if finance_month == "current_month":
            # by default current month and it set to next month to cauclate current month finance
            today = dt.date.today().replace(
                day=calendar.monthrange(today.year, today.month)[1]
            ) + dt.timedelta(days=1)

        first_day_of_last_month = today.replace(day=1) - dt.timedelta(days=1)
        first_day_of_last_month = first_day_of_last_month.replace(day=1)

        billing_log = BillingLog.objects.filter(
            billing_month=first_day_of_last_month,
        ).select_related(
            "client",
            "interviewer",
            "interviewer__user",
            "interview",
            "interview__candidate",
        )
        if request.user.role == Role.CLIENT_OWNER:
            billing_log = billing_log.filter(
                client=request.user.clientuser.organization
            )
            billing_info = BillingRecord.objects.filter(
                client__organization=request.user.clientuser.organization,
                billing_month=first_day_of_last_month,
            ).first()
        elif request.user.role == Role.INTERVIEWER:
            billing_log = billing_log.filter(interviewer__user=request.user)
            billing_info = BillingRecord.objects.filter(
                interviewer__user=request.user,
                billing_month=first_day_of_last_month,
            ).first()
        else:
            if organization_id:
                billing_log = billing_log.filter(
                    candidate__organization_id=organization_id
                )
            else:
                billing_log = billing_log.filter(interviewer_id=interviewer_id)

        if start_date and end_date:
            billing_log = billing_log.filter(
                billing_month__gte=start_date, billing_month__lte=end_date
            )

        paginated_queryset = self.paginate_queryset(billing_log, request)
        if request.user.role == Role.INTERVIEWER:
            serializer = FinanceSerializerForInterviewer(paginated_queryset, many=True)
        else:
            serializer = self.serializer_class(paginated_queryset, many=True)
        paginated_data = self.get_paginated_response(serializer.data)
        response_data = {
            "status": "success",
            "message": "Finance records retreived successfully.",
        }
        if request.user.role in [Role.CLIENT_OWNER, Role.INTERVIEWER]:
            response_data["total_amount"] = (
                billing_info.amount_due if billing_info else 0
            )
            response_data["billing_record_uid"] = (
                billing_info.public_id if billing_info else None
            )
        response_data.update(paginated_data.data)
        return Response(response_data)


class CandidateAnalysisView(APIView):
    serializer_class = AnalyticsQuerySerializer
    permission_classes = [
        IsAuthenticated,
        IsClientOwner
        | IsClientAdmin
        | IsClientUser
        | IsSuperAdmin
        | IsAdmin
        | IsModerator,
    ]

    def get(self, request, job_id):
        user = request.user
        organization = getattr(user.clientuser, "organization", None)

        # Validate role-based org access
        if (
            user.role
            in [
                Role.CLIENT_OWNER,
                Role.CLIENT_ADMIN,
                Role.CLIENT_USER,
            ]
            and not organization
        ):
            return Response(
                {
                    "status": "failed",
                    "message": "organization_id is required.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate query params via serializer
        serializer = self.serializer_class(data=request.query_params)
        if not serializer.is_valid():
            return Response(
                {
                    "status": "failed",
                    "message": "Validation failed.",
                    "errors": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        validated_data = serializer.validated_data
        from_date = timezone.make_aware(
            dt.datetime.combine(validated_data.get("from_date"), dt.datetime.min.time())
        )
        to_date = timezone.make_aware(
            dt.datetime.combine(validated_data.get("to_date"), dt.datetime.max.time())
        )

        if organization_id := validated_data.get("organization_id"):
            try:
                organization = Organization.objects.get(pk=organization_id)
            except ObjectDoesNotExist:
                return Response(
                    {"status": "failed", "message": "Invalid organization_id"}
                )

        if not Job.objects.filter(
            hiring_manager__organization=organization, pk=job_id
        ).exists():
            return Response(
                {"status": "failed", "message": "Invalid job_id in url"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # making the candidate queryset
        candidate = Candidate.objects.filter(
            organization=organization,
            designation__id=job_id,
            created_at__range=(from_date, to_date),
        )

        # Filters provided — return analytics
        analytics_data = get_candidate_analytics(candidate)

        return Response(
            {
                "status": "success",
                "message": "Analytics data retrieved successfully.",
                "data": analytics_data,
            },
            status=status.HTTP_200_OK,
        )


class FeedbackPDFVideoView(APIView):
    serializer_class = FeedbackPDFVideoSerializer
    permission_classes = [
        IsAuthenticated,
        IsClientAdmin | IsClientOwner | IsClientUser | IsAgency,
    ]

    def get(self, request, interview_uid):
        try:
            _, interview_id = force_str(urlsafe_base64_decode(interview_uid)).split(":")
        except (ValueError, TypeError):
            return Response(
                {"status": "failed", "message": "Invalid feedback_uid format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        interview = (
            Interview.objects.filter(pk=interview_id).only("id", "recording").first()
        )
        if not interview:
            return Response(
                {"status": "failed", "message": "Invalid feedback_uid format"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not interview.recording:
            return Response(
                {"status": "failed", "message": "Recording Not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = self.serializer_class(interview)
        return Response(
            {"status": "success", "message": "Recording found", "data": serializer.data}
        )


class BillPaymentView(APIView):
    permission_classes = [IsAuthenticated, IsClientOwner]

    def serialize_obj(self, response_obj):
        if isinstance(response_obj, list):
            return [self.serialize_obj(item) for item in response_obj]
        elif hasattr(response_obj, "__dict__"):
            return {
                key: self.serialize_obj(value)
                for key, value in response_obj.__dict__.items()
            }
        else:
            return response_obj

    def post(self, request, billing_record_uid):

        billing_record = BillingRecord.objects.filter(
            public_id=billing_record_uid
        ).first()
        if not billing_record:
            return Response(
                {
                    "status": "failed",
                    "message": "Billing record not found with the provided billing_record_uid.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        if billing_record.status == "MMP" and billing_record.amount_due == 0:
            return Response(
                {
                    "status": "failed",
                    "message": "Invalid request. As the payment processed already",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        bill_payment = (
            BillPayments.objects.filter(billing_record=billing_record)
            .order_by("-id")
            .first()
        )

        if bill_payment and bill_payment.payment_status == "PED":
            if (
                bill_payment.amount == billing_record.amount_due
                and bill_payment.link_expired_time >= timezone.now()
            ):
                return Response(
                    {
                        "status": "success",
                        "message": "Payment Link retrieved successfully",
                        "data": {
                            "payment_link_url": bill_payment.payment_link_url,
                        },
                    },
                    status=status.HTTP_200_OK,
                )
            else:
                bill_payment.payment_status = "INA"
                bill_payment.save()

        with transaction.atomic():
            user = request.user
            client_profile = user.clientuser
            payment_link_id = f"{user.id}_{uuid.uuid4().hex[:8]}"

            bill_payment = BillPayments.objects.create(
                billing_record=billing_record,
                amount=billing_record.amount_due,
                payment_link_id=payment_link_id,
                customer_name=client_profile.name,
                customer_email=user.email,
                customer_phone=str(user.phone),
                link_expired_time=timezone.now() + timedelta(days=1),
            )

            response = create_payment_link(
                user=user,
                user_name=client_profile.name,
                payment_link_id=payment_link_id,
                amount=float(billing_record.amount_due),
            )
            if not response:
                transaction.set_rollback(True)
                return Response(
                    {"status": "failed", "message": "Error generating payment link"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            elif response.status_code != status.HTTP_200_OK:
                transaction.set_rollback(True)
                print(response.status_code, response.data)
                return Response(
                    {"status": "failed", "message": "Error generating payment link"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            bill_payment.cf_link_id = response.data.cf_link_id
            bill_payment.payment_link_url = response.data.link_url
            bill_payment.meta_data.update(
                {"Create_Response": self.serialize_obj(response.data)}
            )
            bill_payment.save()

        return Response(
            {
                "status": "success",
                "message": "Payment Link Generated Successfully",
                "data": {
                    "payment_link_url": bill_payment.payment_link_url,
                },
            },
            status=status.HTTP_200_OK,
        )


class CFWebhookView(APIView):
    permission_classes = []

    def post(self, request):
        signature = request.headers.get("x-cashfree-signature") or request.headers.get(
            "x-webhook-signature"
        )
        timestamp = request.headers.get("x-cashfree-timestamp") or request.headers.get(
            "x-webhook-timestamp"
        )

        if not signature:
            return Response(
                {"status": "failed", "message": "Missing signature"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not is_valid_signature(request.body.decode("utf-8"), signature, timestamp):
            return Response(
                {"status": "failed", "message": "Invalid signature"},
                status=status.HTTP_403_FORBIDDEN,
            )

        data = request.data.get("data", {})
        order_data = data.get("order", {})

        payment_link_status_map = {
            "PAID": "PAID",
            "PARTIALLY_PAID": "PRT",
            "EXPIRED": "EXP",
            "CANCELLED": "CNL",
        }
        payment_status_map = {
            "SUCCESS": "SUC",
            "FAILED": "FLD",
            "USER_DROPPED": "UDP",
            "CANCELLED": "CNL",
            "VOID": "VOD",
            "PENDING": "PED",
            "INACTIVE": "INA",
        }
        transaction_status = order_data.get("transaction_status", "PENDING")
        transaction_id = order_data.get("transaction_id")
        order_id = order_data.get("order_id")

        link_status = payment_link_status_map.get(data.get("link_status"))
        payment_status = payment_status_map.get(transaction_status)
        bill_payments = BillPayments.objects.filter(payment_link_id=data.get("link_id"))
        bill_payments.update(
            transaction_id=transaction_id,
            payment_status=payment_status,
            order_id=order_id,
            link_status=link_status,
        )
        bill_payment = bill_payments.first()
        if bill_payment and bill_payment.payment_status == "SUC":
            if (
                bill_payment.billing_record.billing_month.month
                == bill_payment.updated_at.month
            ):
                bill_payment.billing_record.amount_due = 0
                bill_payment.billing_record.status = "MMP"
            else:
                bill_payment.billing_record.status = "PAI"
            bill_payment.billing_record.save()

        if bill_payment:
            bill_payment.meta_data.update({"Webhook_Response": data})
            bill_payment.save()

        return Response(
            {"status": "success", "message": "Webhook call received"},
            status=status.HTTP_200_OK,
        )


class PaymentStatusView(APIView):
    permission_classes = [IsAuthenticated, IsClientOwner]

    def get(self, request, payment_link_id):
        bill_payment = BillPayments.objects.filter(
            payment_link_id=payment_link_id
        ).first()
        if bill_payment:
            return Response(
                {
                    "status": "success",
                    "message": "Payment retrived successfully",
                    "data": {
                        "payment_status": bill_payment.payment_status,
                        "amount": bill_payment.amount,
                        "transaction_id": bill_payment.transaction_id,
                    },
                },
                status=status.HTTP_200_OK,
            )
        return Response(
            {"status": "failed", "message": "Payment link not found"},
            status=status.HTTP_404_NOT_FOUND,
        )
