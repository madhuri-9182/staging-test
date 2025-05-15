import datetime
from django.db import transaction
from django.db.models import Q
from django.db.utils import IntegrityError
from django.conf import settings
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.core.exceptions import ObjectDoesNotExist
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import LimitOffsetPagination
from ..serializer import (
    InterviewerAvailabilitySerializer,
    InterviewerRequestSerializer,
    InterviewerDashboardSerializer,
    InterviewFeedbackSerializer,
)
from ..models import (
    InterviewerAvailability,
    Candidate,
    Interview,
    InterviewFeedback,
    InterviewScheduleAttempt,
)
from ..tasks import send_email_to_multiple_recipients, download_feedback_pdf, send_mail
from core.permissions import (
    IsInterviewer,
    IsClientAdmin,
    IsClientUser,
    IsClientOwner,
    IsAgency,
    HasRole,
)
from core.models import OAuthToken, Role
from externals.google.google_calendar import GoogleCalendar
from externals.google.google_meet import create_meet_and_calendar_invite
from hiringdogbackend.utils import get_boolean


CONTACT_EMAIL = settings.EMAIL_HOST_USER if settings.DEBUG else settings.CONTACT_EMAIL
INTERVIEW_EMAIL = (
    settings.EMAIL_HOST_USER if settings.DEBUG else settings.INTERVIEW_EMAIL
)


@extend_schema(tags=["Interviewer"])
class InterviewerAvailabilityView(APIView, LimitOffsetPagination):
    serializer_class = InterviewerAvailabilitySerializer
    permission_classes = [IsAuthenticated, IsInterviewer]

    def post(self, request):
        sync = get_boolean(request.query_params, "sync")
        serializer = self.serializer_class(
            data=request.data, context={"interviewer_user": request.user.interviewer}
        )

        try:
            oauth_obj = OAuthToken.objects.get(user=request.user)
        except OAuthToken.DoesNotExist:
            oauth_obj = None

        if serializer.is_valid():
            with transaction.atomic():
                try:
                    interviewer = serializer.save(interviewer=request.user.interviewer)

                    if oauth_obj and sync:
                        combine_start_datetime = datetime.datetime.combine(
                            interviewer.date, interviewer.start_time
                        )
                        combine_end_datetime = datetime.datetime.combine(
                            interviewer.date, interviewer.end_time
                        )

                        iso_format_start_time = combine_start_datetime.isoformat()
                        iso_format_end_time = combine_end_datetime.isoformat()

                        recurrence = serializer.validated_data.get("recurrence")
                        calender = GoogleCalendar()
                        event_details = {
                            "summary": "Interview Available Time",
                            # "location": "123 Main St, Virtual",
                            # "description": "Discussing project milestones and deadlines.",
                            "start": {
                                "dateTime": iso_format_start_time,
                                "timeZone": "Asia/Kolkata",
                            },
                            "end": {
                                "dateTime": iso_format_end_time,
                                "timeZone": "Asia/Kolkata",
                            },
                            "reminders": {
                                "useDefault": False,
                                "overrides": [],
                            },
                            # "attendees": [
                            #     {"email": "attendee1@example.com"},
                            #     {"email": "attendee2@example.com"},
                            # ],
                        }
                        if recurrence:
                            event_details["recurrence"] = [
                                calender.generate_rrule_string(recurrence)
                            ]

                        event = calender.create_event(
                            access_token=oauth_obj.access_token,
                            refresh_token=oauth_obj.refresh_token,
                            user=request.user,
                            event_details=event_details,
                        )
                        interviewer.google_calendar_id = event.pop("id", "")
                        interviewer.save()

                except Exception as e:
                    transaction.set_rollback(True)
                    return Response(
                        {
                            "status": "failed",
                            "message": "Something went wrong while creating the event.",
                            "error": str(e),
                        },
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    )

            return Response(
                {
                    "status": "success",
                    "message": "Interviewer Availability added successfully.",
                    "data": serializer.data,
                    "event_details": event if oauth_obj and sync else None,
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

    def get(self, request):
        today_date = datetime.datetime.now().date()
        interviewer_avi_qs = InterviewerAvailability.objects.filter(
            interviewer=request.user.interviewer, date__gte=today_date
        )

        serializer = self.serializer_class(interviewer_avi_qs, many=True)

        return_response = {
            "status": "success",
            "message": "Successfully retrieve the availability.",
            "results": serializer.data,
        }

        return Response(
            return_response,
            status=status.HTTP_200_OK,
        )


@extend_schema(tags=["Interviewer"])
class InterviewerReqeustView(APIView):
    serializer_class = InterviewerRequestSerializer
    permission_classes = [
        IsAuthenticated,
        IsClientUser | IsClientAdmin | IsClientOwner | IsAgency,
    ]

    def post(self, request):
        with transaction.atomic():
            serializer = self.serializer_class(
                data=request.data, context={"request": request}
            )
            if serializer.is_valid():
                candidate_id = serializer.validated_data["candidate_id"]
                interviewer_ids = serializer.validated_data["interviewer_ids"]
                candidate = serializer.validated_data.pop("candidate_obj")
                contexts = []

                scheduling_attempt = InterviewScheduleAttempt.objects.create(
                    candidate=candidate
                )

                # rescheduling when candidate is scheduled already to an interviewer
                if candidate.status == "CSCH":
                    candidate.status = "NSCH"
                    interview_obj = (
                        Interview.objects.select_for_update()
                        .filter(candidate=candidate)
                        .order_by("-id")
                        .first()
                    )
                    interview_obj.status = "RESCH"
                    scheduled_time = interview_obj.scheduled_time
                    interviewer = interview_obj.interviewer
                    if hasattr(interview_obj, "availability"):
                        interview_obj.availability.booked_by = None
                        interview_obj.availability.is_scheduled = False
                        interview_obj.availability.save()
                    interview_obj.save()
                    candidate.save()

                    send_mail.delay(
                        to=interviewer.email,
                        subject=f"Interview with {candidate.name} has been cancelled",
                        template="client_interview_cancelled_notification.html",
                        candidate_name=candidate.name,
                        interviewer_name=interviewer.name,
                        interview_date=timezone.localtime(scheduled_time)
                        .date()
                        .strftime("%d/%m/%Y"),
                        interview_time=timezone.localtime(scheduled_time)
                        .time()
                        .strftime("%I:%M %p"),
                    )

                    send_mail.delay(
                        to=candidate.email,
                        subject=f"{candidate.name}, Your Interview Has Been Cancelled",
                        template="client_candidate_cancelled_notification.html",
                        candidate_name=candidate.name,
                        interview_date=timezone.localtime(scheduled_time)
                        .date()
                        .strftime("%d/%m/%Y"),
                        interview_time=timezone.localtime(scheduled_time)
                        .time()
                        .strftime("%I:%M %p"),
                    )

                for interviewer_obj in InterviewerAvailability.objects.filter(
                    pk__in=interviewer_ids, booked_by__isnull=True
                ).select_related("interviewer"):
                    schedule_datetime = datetime.datetime.combine(
                        serializer.validated_data.get("date"),
                        serializer.validated_data.get("time"),
                    )
                    data = f"interviewer_avialability_id:{interviewer_obj.id};candidate_id:{candidate_id};schedule_time:{schedule_datetime};booked_by:{request.user.id};expired_time:{datetime.datetime.now()+datetime.timedelta(hours=1)};scheduling_id:{scheduling_attempt.id}"
                    accept_data = data + ";action:accept"
                    reject_data = data + ";action:reject"
                    accept_uid = urlsafe_base64_encode(force_bytes(accept_data))
                    reject_uid = urlsafe_base64_encode(force_bytes(reject_data))
                    context = {
                        "name": interviewer_obj.interviewer.name,
                        "email": interviewer_obj.interviewer.email,
                        "interview_date": serializer.validated_data["date"],
                        "interview_time": serializer.validated_data["time"],
                        "position": candidate.designation.get_name_display(),
                        "site_domain": settings.SITE_DOMAIN,
                        "accept_link": "/confirmation/{}/".format(accept_uid),
                        "reject_link": "/confirmation/{}/".format(reject_uid),
                        "from_email": INTERVIEW_EMAIL,
                    }
                    contexts.append(context)

                send_email_to_multiple_recipients.delay(
                    contexts,
                    "Interview Opportunity Available - Confirm Your Availability",
                    "interviewer_interview_notification.html",
                )
                candidate.last_scheduled_initiate_time = timezone.now()
                candidate.status = "SCH"
                candidate.save()
                return Response(
                    {
                        "status": "success",
                        "message": "Scheduling initiated successfully. Interviewers will be notified shortly.",
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


@extend_schema(tags=["Interviewer"])
class InterviewerRequestResponseView(APIView):
    serializer_class = None

    def post(self, request, request_id):
        try:
            with transaction.atomic():
                try:
                    decode_data = force_str(urlsafe_base64_decode(request_id))
                    data_parts = decode_data.split(";")
                    if len(data_parts) != 7:
                        raise ValueError("Invalid data format")

                    (
                        interviewer_availability_id,
                        candidate_id,
                        schedule_time,
                        booked_by,
                        expired_time,
                        scheduling_id,
                        action,
                    ) = [item.split(":", 1)[1] for item in data_parts]
                except Exception:
                    return Response(
                        {"status": "failed", "message": "Invalid Request ID format."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                expired_time = datetime.datetime.strptime(
                    expired_time, "%Y-%m-%d %H:%M:%S.%f"
                )
                if datetime.datetime.now() > expired_time:
                    return Response(
                        {"status": "failed", "message": "Request expired"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                interviewer_availability = (
                    InterviewerAvailability.objects.select_for_update()
                    .filter(pk=interviewer_availability_id)
                    .first()
                )
                candidate = (
                    Candidate.objects.select_for_update()
                    .filter(pk=candidate_id)
                    .first()
                )

                if candidate.status == "SCH":
                    try:
                        scheduling_attempts = candidate.scheduling_attempts.latest(
                            "created_at"
                        )
                    except ObjectDoesNotExist:
                        scheduling_attempts = None
                    if scheduling_attempts and scheduling_id != str(
                        scheduling_attempts.id
                    ):
                        return Response(
                            {
                                "status": "failed",
                                "message": "This interview schedule has expired or was cancelled.",
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )

                if not interviewer_availability or not candidate:
                    return Response(
                        {
                            "status": "failed",
                            "message": "Invalid Interviewer or Candidate.",
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                if candidate.status == "CSCH":
                    return Response(
                        {
                            "status": "failed",
                            "message": "The candidate is currently occupied and has already been assigned to an interviewer.",
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                if candidate.status not in ["SCH", "NSCH"]:
                    return Response(
                        {"status": "failed", "message": "Invalid request"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                schedule_time = datetime.datetime.strptime(
                    schedule_time, "%Y-%m-%d %H:%M:%S"
                )
                schedule_time = timezone.make_aware(schedule_time)

                # To handle multiple interview requests from different clients to the same interviewer scenario
                schedule_time_after_one_hour = schedule_time + datetime.timedelta(
                    hours=1
                )
                schedule_time_before_one_hour = schedule_time - datetime.timedelta(
                    hours=1
                )
                if (
                    Interview.objects.select_for_update()
                    .filter(
                        interviewer=interviewer_availability.interviewer,
                        status="CSCH",
                    )
                    .filter(
                        Q(scheduled_time=schedule_time)
                        | Q(
                            scheduled_time__gte=schedule_time_before_one_hour,
                            scheduled_time__lt=schedule_time,
                        )
                        | Q(
                            scheduled_time__lte=schedule_time_after_one_hour,
                            scheduled_time__gt=schedule_time,
                        )
                    )
                    .exists()
                ):
                    return Response(
                        {
                            "status": "failed",
                            "message": "There must be a 1-hour gap between two consecutive scheduled interviews.",
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                if action == "accept":
                    try:
                        interview_obj = (
                            Interview.objects.select_for_update()
                            .filter(candidate=candidate)
                            .order_by("-id")
                            .first()
                        )

                        interview = Interview.objects.create(
                            candidate=candidate,
                            interviewer=interviewer_availability.interviewer,
                            status="CSCH",
                            scheduled_time=schedule_time,
                            total_score=100,
                            previous_interview=interview_obj,
                            availability=interviewer_availability,
                        )
                    except IntegrityError as e:
                        print(str(e))
                        return Response(
                            {
                                "status": "failed",
                                "message": "Interviewer already has a scheduled interview at this time.",
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    interviewer_availability.booked_by_id = booked_by
                    interviewer_availability.is_scheduled = True

                    original_start_time = interviewer_availability.start_time
                    original_end_time = interviewer_availability.end_time

                    # updating with the booked time
                    interviewer_availability.start_time = schedule_time.time()
                    interviewer_availability.end_time = (
                        schedule_time + datetime.timedelta(hours=1)
                    ).time()
                    interviewer_availability.save()

                    # creating new available instance for if interviewer is futher available with 1hour before and after time gap
                    original_availability_date = interviewer_availability.date
                    new_slots = []
                    before_slot_end = (
                        schedule_time - datetime.timedelta(hours=1)
                    ).time()
                    before_slot_start = original_start_time
                    before_slot_end_dt = datetime.datetime.combine(
                        original_availability_date, before_slot_end
                    )
                    before_slot_start_dt = datetime.datetime.combine(
                        original_availability_date, before_slot_start
                    )
                    if (
                        before_slot_end_dt - before_slot_start_dt
                    ) >= datetime.timedelta(hours=1):
                        new_slots.append(
                            InterviewerAvailability(
                                interviewer=interviewer_availability.interviewer,
                                date=interviewer_availability.date,
                                start_time=before_slot_start,
                                end_time=before_slot_end,
                                google_calendar_id=interviewer_availability.google_calendar_id,
                            )
                        )
                    after_slot_start = (
                        schedule_time + datetime.timedelta(hours=2)
                    ).time()
                    after_slot_end = original_end_time

                    after_slot_start_dt = datetime.datetime.combine(
                        original_availability_date, after_slot_start
                    )
                    after_slot_end_dt = datetime.datetime.combine(
                        original_availability_date, after_slot_end
                    )
                    if (after_slot_end_dt - after_slot_start_dt) >= datetime.timedelta(
                        hours=1
                    ):
                        new_slots.append(
                            InterviewerAvailability(
                                interviewer=interviewer_availability.interviewer,
                                date=interviewer_availability.date,
                                start_time=after_slot_start,
                                end_time=after_slot_end,
                                google_calendar_id=interviewer_availability.google_calendar_id,
                            )
                        )

                    InterviewerAvailability.objects.bulk_create(new_slots)

                    # sending the confirmation notification
                    interview_date = schedule_time.date().strftime("%d/%m/%Y")
                    interview_time = schedule_time.time().strftime("%H:%M:%S")

                    meeting_link, event_id = create_meet_and_calendar_invite(
                        interviewer_availability.interviewer.email,
                        candidate.email,
                        schedule_time,
                        schedule_time + datetime.timedelta(hours=1),
                        candidate_name=candidate.name,
                        designation_name=candidate.designation.get_name_display(),
                    )

                    interview.scheduled_service_account_event_id = event_id
                    interview.meeting_link = meeting_link
                    interview.save()

                    internal_user = candidate.organization.internal_client.assigned_to

                    contexts = [
                        {
                            "name": candidate.name,
                            "position": candidate.designation.get_name_display(),
                            "company_name": candidate.organization.name,
                            "interview_date": interview_date,
                            "interview_time": interview_time,
                            "interviewer": interviewer_availability.interviewer.name,
                            "email": candidate.email,
                            "template": "interview_confirmation_candidate_notification.html",
                            "recruiter_email": candidate.added_by.user.email,
                            "subject": f"Interview Scheduled - {candidate.designation.get_name_display()}",
                            "meeting_link": meeting_link,
                            "from_email": INTERVIEW_EMAIL,
                        },
                        {
                            "name": interviewer_availability.interviewer.name,
                            "position": candidate.designation.get_name_display(),
                            "interview_date": interview_date,
                            "interview_time": interview_time,
                            "candidate": candidate.name,
                            "email": interviewer_availability.interviewer.email,
                            "template": "interview_confirmation_interviewer_notification.html",
                            "subject": f"Interview Assigned - {candidate.name}",
                            "meeting_link": meeting_link,
                            "from_email": INTERVIEW_EMAIL,
                        },
                        {
                            "name": candidate.organization.name,
                            "position": candidate.designation.get_name_display(),
                            "interview_date": interview_date,
                            "interview_time": interview_time,
                            "candidate": candidate.name,
                            "email": getattr(
                                getattr(candidate.added_by, "user", None),
                                "email",
                                candidate.designation.hiring_manager.user.email,
                            ),
                            "template": "interview_confirmation_client_notification.html",
                            "subject": f"Interview Scheduled - {candidate.name}",
                            "meeting_link": meeting_link,
                            "from_email": INTERVIEW_EMAIL,
                        },
                        {
                            "organization_name": candidate.organization.name,
                            "internal_user_name": internal_user.name,
                            "position": candidate.designation.get_name_display(),
                            "interview_date": interview_date,
                            "interview_time": interview_time,
                            "candidate_name": candidate.name,
                            "email": internal_user.user.email,
                            "template": "internal_interview_scheduling_confirmation.html",
                            "subject": f"Interview Scheduled - {candidate.name}",
                            "meeting_link": meeting_link,
                            "from_email": INTERVIEW_EMAIL,
                        },
                    ]

                    send_email_to_multiple_recipients.delay(
                        contexts,
                        "",
                        "",
                    )

                    return Response(
                        {"status": "success", "message": "Interview Confirmed"},
                        status=status.HTTP_200_OK,
                    )

                return Response(
                    {"status": "success", "message": "Interview Rejected"},
                    status=status.HTTP_200_OK,
                )
        except Exception as e:
            return Response(
                {"status": "failed", "message": f"Error: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )


class InterviewerAcceptedInterviewsView(APIView, LimitOffsetPagination):
    serializer_class = InterviewerDashboardSerializer
    permission_classes = (IsAuthenticated, IsInterviewer)

    def get(self, request):
        accepted_interviews_qs = Interview.objects.filter(
            interviewer=request.user.interviewer,
            status="CSCH",
            scheduled_time__gte=timezone.now() - datetime.timedelta(hours=1),
        ).select_related("candidate", "candidate__designation")
        paginated_queryset = self.paginate_queryset(accepted_interviews_qs, request)
        serializer = self.serializer_class(paginated_queryset, many=True)
        paginated_data = self.get_paginated_response(serializer.data)
        return Response(
            {
                "status": "success",
                "message": "Accepted interviews fetched successfully",
                **paginated_data.data,
            },
            status=status.HTTP_200_OK,
        )


class InterviewerPendingFeedbackView(APIView, LimitOffsetPagination):
    serializer_class = InterviewerDashboardSerializer
    permission_classes = (IsAuthenticated, IsInterviewer)

    def get(self, request):
        pending_feedback_qs = Interview.objects.filter(
            interviewer=request.user.interviewer,
            interview_feedback__is_submitted=False,
        ).select_related("candidate", "candidate__designation")

        paginated_queryset = self.paginate_queryset(pending_feedback_qs, request)
        serializer = self.serializer_class(paginated_queryset, many=True)
        paginated_data = self.get_paginated_response(serializer.data)
        return Response(
            {
                "status": "success",
                "message": "Pending feedback fetched successfully",
                **paginated_data.data,
            },
            status=status.HTTP_200_OK,
        )


class InterviewerInterviewHistoryView(APIView, LimitOffsetPagination):
    serializer_class = InterviewerDashboardSerializer
    permission_classes = (IsAuthenticated, IsInterviewer)

    def get(self, request):
        interview_history_qs = Interview.objects.filter(
            interviewer=request.user.interviewer, interview_feedback__is_submitted=True
        ).select_related("candidate", "candidate__designation")

        paginated_queryset = self.paginate_queryset(interview_history_qs, request)
        serializer = self.serializer_class(paginated_queryset, many=True)
        paginated_data = self.get_paginated_response(serializer.data)
        return Response(
            {
                "status": "success",
                "message": "Interview history fetched successfully",
                **paginated_data.data,
            },
            status=status.HTTP_200_OK,
        )


class InterviewFeedbackView(APIView, LimitOffsetPagination):
    serializer_class = InterviewFeedbackSerializer
    permission_classes = (IsAuthenticated, HasRole)
    roles_mapping = {
        "GET": [
            Role.INTERVIEWER,
            Role.CLIENT_ADMIN,
            Role.CLIENT_OWNER,
            Role.CLIENT_USER,
            Role.AGENCY,
        ],
        "PATCH": [Role.INTERVIEWER],
        "POST": [Role.INTERVIEWER],
    }

    def get(self, request, interview_id=None):
        if not interview_id:
            return Response(
                {"status": "failed", "message": "Interview id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        interview_feedback_qs = (
            InterviewFeedback.objects.filter(interview_id=interview_id)
            .select_related(
                "interview",
                "interview__candidate",
                "interview__candidate__designation",
                "interview__interviewer",
            )
            .order_by("-id")
        )
        if request.user.role != Role.INTERVIEWER and request.method == "GET":
            interview_feedback_qs = interview_feedback_qs.filter(
                interview__candidate__organization=request.user.clientuser.organization
            )
        if not interview_feedback_qs.exists():
            return Response(
                {
                    "status": "failed",
                    "message": "No interview feedback found for current interview id",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = self.serializer_class(interview_feedback_qs.first())
        return Response(
            {
                "status": "success",
                "message": "Interview feedback fetched successfully",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    # def post(self, request):
    #     serializer = self.serializer_class(data=request.data)
    #     serializer.is_valid(raise_exception=True)
    #     interview_id = serializer.validated_data.get("interview_id")
    #     if interview_id:
    #         if InterviewFeedback.objects.filter(interview_id=interview_id).exists():
    #             return Response(
    #                 {
    #                     "status": "failed",
    #                     "message": "Interview feedback for this interview already exists",
    #                 },
    #                 status=status.HTTP_400_BAD_REQUEST,
    #             )
    #     serializer.save(is_submitted=True)
    #     return Response(
    #         {
    #             "status": "success",
    #             "message": "Interview feedback added successfully.",
    #             "data": serializer.data,
    #         },
    #         status=status.HTTP_201_CREATED,
    #     )

    def patch(self, request, interview_id=None):
        if not interview_id:
            return Response(
                {"status": "failed", "message": "Interview id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        interview_feedback_qs = (
            InterviewFeedback.objects.filter(interview_id=interview_id)
            .select_related("interview")
            .order_by("-id")
        )
        if not interview_feedback_qs.exists():
            return Response(
                {
                    "status": "failed",
                    "message": "No interview feedback found for current interview id",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        interview_feedback = interview_feedback_qs.first()
        if interview_feedback.is_submitted:
            return Response(
                {
                    "status": "failed",
                    "message": "Invalid request. Feedback is already submitted.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = self.serializer_class(
            interview_feedback, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(is_submitted=True)
        download_feedback_pdf.delay(interview_feedback.interview.id)
        internal_user = (
            interview_feedback.interview.candidate.organization.internal_client.assigned_to
        )
        interviewer_name = interview_feedback.interview.interviewer.name
        candidate_name = interview_feedback.interview.candidate.name
        position = interview_feedback.interview.candidate.designation.get_name_display()
        recruiter = interview_feedback.interview.candidate.added_by
        contexts = [
            {
                "internal_user_name": internal_user.name,
                "from_email": settings.EMAIL_HOST_USER,
                "email": internal_user.user.email,
                "organization_name": interview_feedback.interview.candidate.organization.name,
                "candidate_name": candidate_name,
                "interviewer_name": interviewer_name,
                "position": position,
                "interview_date": interview_feedback.interview.scheduled_time.strftime(
                    "%d/%m/%Y %H:%M"
                ),
                "subject": f"Feedback Submitted: Insights from {interviewer_name} on {candidate_name}",
                "template": "internal_interview_submitted_feedback_notification.html",
            }
        ]
        if recruiter:
            contexts.append(
                {
                    "client_name": recruiter.name,
                    "candidate_name": candidate_name,
                    "subject": f"📝 Feedback Alert: SDE1 Interview Feedback for {candidate_name} is Now Available",
                    "interviewer_name": interviewer_name,
                    "from_email": settings.EMAIL_HOST_USER,
                    "email": recruiter.user.email,
                    "template": "client_interview_feedback_submitted_notification.html",
                }
            )
        send_email_to_multiple_recipients.delay(contexts, "", "")
        return Response(
            {
                "status": "success",
                "message": "Interview feedback updated successfully.",
                "data": serializer.data,
            },
            status=status.HTTP_201_CREATED,
        )
