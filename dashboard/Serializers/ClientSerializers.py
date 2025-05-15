import json
from datetime import datetime
from celery import group
from rest_framework import serializers
from celery import group
from django.conf import settings
from django.db import transaction
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from core.models import User, Role
from datetime import date
from ..models import (
    ClientUser,
    Job,
    Candidate,
    InternalInterviewer,
    Engagement,
    EngagementOperation,
    EngagementTemplates,
    Interview,
    BillingLog,
)
from phonenumber_field.serializerfields import PhoneNumberField
from hiringdogbackend.utils import (
    validate_incoming_data,
    get_random_password,
    check_for_email_and_phone_uniqueness,
    validate_attachment,
    validate_json,
)
from ..tasks import send_mail, send_schedule_engagement_email


class ClientUserDetailsSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "email", "phone", "role")


class JobSpecificDetailsSerializer(serializers.ModelSerializer):
    class Meta:
        model = Job
        fields = ("id", "name")


class ClientUserSerializer(serializers.ModelSerializer):
    created_at = serializers.DateTimeField(format="%d/%m/%Y", read_only=True)
    user = ClientUserDetailsSerializer(read_only=True)
    email = serializers.EmailField(write_only=True, required=False)
    role = serializers.ChoiceField(
        choices=Role.choices, write_only=True, required=False
    )
    phone = PhoneNumberField(write_only=True, required=False)
    jobs_assigned = serializers.ListField(
        child=serializers.IntegerField(), required=False, write_only=True
    )
    assigned_jobs = JobSpecificDetailsSerializer(
        read_only=True, many=True, source="jobs"
    )
    accessibility = serializers.ChoiceField(
        choices=ClientUser.ACCESSIBILITY_CHOICES,
        error_messages={
            "invalid_choice": f"This is an invalid choice. Valid choices are {', '.join([f'{key}({value})' for key, value in ClientUser.ACCESSIBILITY_CHOICES])}"
        },
        required=False,
    )

    class Meta:
        model = ClientUser
        fields = (
            "id",
            "user",
            "name",
            "email",
            "phone",
            "role",
            "designation",
            "jobs_assigned",
            "assigned_jobs",
            "created_at",
            "accessibility",
        )
        read_only_fields = ["created_at"]

    def run_validation(self, data=...):
        email = data.get("email")
        phone_number = data.get("phone")
        role = data.get("role")
        errors = check_for_email_and_phone_uniqueness(email, phone_number, User)
        if role and role not in ("client_user", "client_admin", "agency"):
            errors.setdefault("role", []).append("Invalid role type.")
        if errors:
            raise serializers.ValidationError({"errors": errors})
        return super().run_validation(data)

    def validate(self, data):
        errors = validate_incoming_data(
            self.initial_data,
            [
                "name",
                "email",
                "role",
                "phone",
                "accessibility",
            ],
            allowed_keys=["jobs_assigned"],
            partial=self.partial,
        )

        if errors:
            raise serializers.ValidationError({"errors": errors})

        return data

    def create(self, validated_data):
        email = validated_data.pop("email", None)
        phone_number = validated_data.pop("phone", None)
        user_role = validated_data.pop("role", None)
        name = validated_data.get("name")
        organization = validated_data.get("organization")
        jobs_assigned = validated_data.pop("jobs_assigned", None)
        temp_password = get_random_password()
        current_user = self.context.get("user")

        with transaction.atomic():
            user = User.objects.create_user(
                email=email, phone=phone_number, password=temp_password, role=user_role
            )
            user.profile.name = name
            user.profile.organization = organization
            user.profile.save()

            client_user = ClientUser.objects.create(user=user, **validated_data)
            if jobs_assigned:
                job_qs = Job.objects.filter(pk__in=jobs_assigned)
                client_user.jobs.add(*job_qs)

            data = f"user:{current_user.email};invitee-email:{email}"
            uid = urlsafe_base64_encode(force_bytes(data))
            send_mail_to_clientuser = send_mail.si(
                to=email,
                subject=f"You're Invited to Join {organization.name} on Hiring Dog",
                template="invitation.html",
                invited_name=name,
                user_name=current_user.clientuser.name,
                user_email=current_user.email,
                org_name=organization.name,
                password=temp_password,
                login_url=settings.LOGIN_URL,
                activation_url=f"/client/client-user-activate/{uid}/",
                site_domain=settings.SITE_DOMAIN,
            )

            send_mail_to_internal = send_mail.si(
                to=organization.internal_client.assigned_to.user.email,
                subject=f"Confirmation: Invitation Sent to {name} for {organization.name}",
                template="internal_client_clientuser_invitation_confirmation.html",
                internal_user_name=organization.internal_client.assigned_to.name,
                client_user_name=name,
                invitation_date=datetime.today().strftime("%d/%m/%Y"),
                client_name=organization.name,
            )

            transaction.on_commit(
                lambda: (send_mail_to_clientuser | send_mail_to_internal).apply_async()
            )

        return client_user

    def update(self, instance, validated_data):
        email = validated_data.pop("email", None)
        phone_number = validated_data.pop("phone", None)
        role = validated_data.pop("role", None)
        name = validated_data.get("name")
        jobs_assigned = validated_data.pop("jobs_assigned", None)

        updated_client_user = super().update(instance, validated_data)

        if jobs_assigned:
            jobs_qs = Job.objects.filter(pk__in=jobs_assigned)
            updated_client_user.jobs.set(jobs_qs)

        if email:
            updated_client_user.user.email = email
        if phone_number:
            updated_client_user.user.phone = phone_number
        if role:
            updated_client_user.user.role = role
        if name:
            updated_client_user.user.profile.name = name
            updated_client_user.user.profile.save()

        updated_client_user.user.save()
        return updated_client_user


class JobClientSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientUser
        fields = ("id", "name")


class JobSerializer(serializers.ModelSerializer):
    clients = JobClientSerializer(read_only=True, many=True)
    hiring_manager = JobClientSerializer(read_only=True)
    recruiter_ids = serializers.CharField(write_only=True, required=False)
    hiring_manager_id = serializers.IntegerField(write_only=True, required=False)
    interview_time = serializers.TimeField(input_formats=["%H:%M:%S"], required=False)
    name = serializers.ChoiceField(
        choices=InternalInterviewer.ROLE_CHOICES,
        error_messages={
            "invalid_choice": f"This is an invalid choice. Valid choices are {', '.join([f'{key}({value})' for key, value in InternalInterviewer.ROLE_CHOICES])}"
        },
        required=False,
    )
    specialization = serializers.ChoiceField(
        choices=Candidate.SPECIALIZATION_CHOICES,
        error_messages={
            "invalid_choice": f"This is an invalid choice. Valid choices are {', '.join([f'{key}({value})' for key, value in Candidate.SPECIALIZATION_CHOICES])}"
        },
        required=False,
    )
    active_candidates = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = (
            "id",
            "clients",
            "name",
            "job_id",
            "hiring_manager",
            "recruiter_ids",
            "hiring_manager_id",
            "total_positions",
            "job_description_file",
            "mandatory_skills",
            "interview_time",
            "other_details",
            "reason_for_archived",
            "specialization",
            "active_candidates",
            "is_diversity_hiring",
        )

    def run_validation(self, data=...):
        valid_reasons = ["PF", "POH", "OTH"]
        reason = data.get("reason_for_archived")

        if reason and reason not in valid_reasons:
            raise serializers.ValidationError(
                {
                    "errors": {
                        "reason_for_archived": ["Invalid reason_for_archived value."]
                    }
                }
            )
        return super().run_validation(data)

    def validate(self, data):
        org = self.context["org"]

        required_keys = [
            "name",
            "hiring_manager_id",
            "recruiter_ids",
            "total_positions",
            "job_description_file",
            "mandatory_skills",
            "specialization",
        ]
        allowed_keys = [
            "job_id",
            "reason_for_archived",
            "other_details",
            "interview_time",
            "is_diversity_hiring",
        ]

        errors = validate_incoming_data(
            self.initial_data,
            required_keys,
            allowed_keys,
            original_data=data,
            form=True,
            partial=self.partial,
        )
        if errors:
            raise serializers.ValidationError({"errors": errors})

        hiring_manager_id = data.get("hiring_manager_id")
        recruiter_ids = data.get("recruiter_ids")

        client_user_ids = set(
            ClientUser.objects.filter(organization=org).values_list("id", flat=True)
        )
        if recruiter_ids:
            try:
                recruiter_ids = set(json.loads(recruiter_ids))
                if not recruiter_ids.issubset(client_user_ids):
                    errors.setdefault("recruiter_ids", []).append(
                        f"Invalid recruiter_ids(clientuser_ids): {recruiter_ids - client_user_ids}"
                    )

            except (json.JSONDecodeError, ValueError, TypeError):
                errors.setdefault("recruiter_ids", []).append(
                    "Invalid data format. It should be a list of integers."
                )

        if hiring_manager_id and hiring_manager_id not in client_user_ids:
            errors.setdefault("hiring_manager_id", []).append(
                "Invalid hiring_manager_id"
            )
        if (
            hiring_manager_id
            and isinstance(recruiter_ids, list)
            and hiring_manager_id in recruiter_ids
        ):
            errors.setdefault("conflict_error", []).append(
                "hiring_manager_id and recruiter_id cannot be the same."
            )

        if data.get("total_positions") and not (0 <= data.get("total_positions") < 100):
            errors.setdefault("total_positions", []).append("Invalid total_positions")

        if data.get("interview_time"):
            try:
                datetime.strptime(str(data["interview_time"]), "%H:%M:%S")
            except (ValueError, TypeError):
                errors.setdefault("interview_time", []).append(
                    "Invalid interview time. Format should be %H:%M:%S"
                )

        if data.get("job_description_file"):
            error = validate_attachment(
                "job_description_file",
                data["job_description_file"],
                ["doc", "docx", "pdf"],
                max_size_mb=5,
            )
            if error:
                errors.update(error)

        if data.get("other_details") is not None:
            schema = {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "details": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "time": {
                            "type": "string",
                            "pattern": "^\\d+min$",
                        },
                        "guidelines": {
                            "type": "string",
                            "minLength": 1,
                        },
                    },
                    "required": ["details", "time", "guidelines"],
                },
            }
            errors.update(validate_json(data["other_details"], "other_details", schema))

        if data.get("mandatory_skills") is not None:
            schema = {"type": "array", "items": {"type": "string"}, "minItems": 1}
            errors.update(
                validate_json(data["mandatory_skills"], "mandatory_skills", schema)
            )

        if errors:
            raise serializers.ValidationError({"errors": errors})
        data["recruiter_ids"] = recruiter_ids
        return data

    def get_active_candidates(self, obj):
        return obj.candidate.count()

    def create(self, validated_data):
        recruiter_ids = validated_data.pop("recruiter_ids")
        job = super().create(validated_data)
        job.clients.add(*recruiter_ids)
        return job

    def update(self, instance, validated_data):
        recruiter_ids = validated_data.pop("recruiter_ids", None)
        job = super().update(instance, validated_data)
        if recruiter_ids is not None:
            job.clients.set(recruiter_ids)
        return job


class CandidateSerializer(serializers.ModelSerializer):
    designation = JobSpecificDetailsSerializer(read_only=True)
    gender = serializers.ChoiceField(
        choices=Candidate.GENDER_CHOICES,
        error_messages={
            "invalid_choice": f"This is an invalid choice. Valid choices are {', '.join([f'{key}({value})' for key, value in Candidate.GENDER_CHOICES])}"
        },
        required=False,
    )
    source = serializers.ChoiceField(
        choices=Candidate.SOURCE_CHOICES,
        error_messages={
            "invalid_choice": f"This is an invalid choice. Valid choices are {', '.join([f'{key}({value})' for key, value in Candidate.SOURCE_CHOICES])}"
        },
        required=False,
    )
    status = serializers.ChoiceField(
        choices=Candidate.STATUS_CHOICES,
        error_messages={
            "invalid_choice": f"This is an invalid choice. Valid choices are {', '.join([f'{key}({value})' for key, value in Candidate.STATUS_CHOICES])}"
        },
        required=False,
    )
    final_selection_status = serializers.ChoiceField(
        choices=Candidate.FINAL_SELECTION_STATUS_CHOICES,
        error_messages={
            "invalid_choice": f"This is an invalid choice. Valid choices are {', '.join([f'{key}({value})' for key, value in Candidate.FINAL_SELECTION_STATUS_CHOICES])}"
        },
        required=False,
    )
    reason_for_dropping = serializers.ChoiceField(
        choices=Candidate.REASON_FOR_DROPPING_CHOICES,
        error_messages={
            "invalid_choice": f"This is an invalid choice. Valid choices are {', '.join([f'{key}({value})' for key, value in Candidate.REASON_FOR_DROPPING_CHOICES])}"
        },
        required=False,
    )
    job_id = serializers.IntegerField(required=False, write_only=True)
    specialization = serializers.ChoiceField(
        choices=Candidate.SPECIALIZATION_CHOICES,
        error_messages={
            "invalid_choice": f"This is an invalid choice. Valid choices are {', '.join([f'{key}({value})' for key, value in Candidate.SPECIALIZATION_CHOICES])}"
        },
        required=False,
    )
    created_at = serializers.DateTimeField(format="%d/%m/%Y", read_only=True)

    class Meta:
        model = Candidate
        fields = (
            "id",
            "name",
            "designation",
            "source",
            "year",
            "month",
            "cv",
            "status",
            "gender",
            "score",
            "total_score",
            "final_selection_status",
            "email",
            "phone",
            "company",
            "current_designation",
            "specialization",
            "remark",
            "last_scheduled_initiate_time",
            "reason_for_dropping",
            "job_id",
            "created_at",
            "is_engagement_pushed",
            "interviews",
        )
        read_only_fields = ["designation", "created_at", "is_engagement_pushed"]

    def validate(self, data):
        request = self.context.get("request")
        required_keys = [
            "name",
            "year",
            "month",
            "phone",
            "email",
            "company",
            "current_designation",
            "job_id",
            "source",
            "cv",
            "specialization",
        ]
        allowed_keys = [
            "status",
            "reason_for_dropping",
            "remark",
            "gender",
        ]

        if self.partial:
            allowed_keys = [
                "specialization",
                "remark",
                "source",
                "final_selection_status",
            ]
            required_keys = allowed_keys

        errors = validate_incoming_data(
            self.initial_data,
            required_keys,
            allowed_keys,
            partial=self.partial,
            original_data=data,
            form=True,
        )

        if errors:
            raise serializers.ValidationError({"errors": errors})

        job = Job.objects.filter(
            pk=data.get("job_id"),
            hiring_manager__organization=request.user.clientuser.organization,
        ).first()
        if data.get("job_id") and not job:
            errors.setdefault("job_id", []).append("Invalid job_id")

        if job and job.is_diversity_hiring and not data.get("gender"):
            errors.setdefault("gender", []).append(
                "This is required field for diversity hiring."
            )

        if data.get("cv"):
            errors.update(validate_attachment("cv", data.get("cv"), ["pdf", "docx"], 5))
        if errors:
            raise serializers.ValidationError({"errors": errors})

        return data


class EngagementOperationDataSerializer(serializers.Serializer):
    template_id = serializers.IntegerField()
    date = serializers.DateTimeField(
        input_formats=["%d/%m/%Y %H:%M:%S"], format="%d/%m/%Y %H:%M"
    )
    week = serializers.IntegerField(min_value=1, max_value=12)


class EngagementOperationTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = EngagementTemplates
        fields = ("id", "template_name")


class EngagementOperationSerializer(serializers.ModelSerializer):
    template_data = EngagementOperationDataSerializer(
        many=True, write_only=True, required=False
    )
    template = EngagementOperationTemplateSerializer(read_only=True)
    engagement_id = serializers.IntegerField(write_only=True, required=False)
    date = serializers.DateTimeField(format="%d/%m/%Y %H:%M:%S", read_only=True)

    class Meta:
        model = EngagementOperation
        fields = (
            "id",
            "template",
            "week",
            "date",
            "delivery_status",
            "engagement_id",
            "template_data",
            "operation_complete_status",
        )
        read_only_fields = [
            "week",
            "date",
            "delivery_status",
            "operation_complete_status",
        ]

    def to_internal_value(self, data):
        template_data = data.get("template_data", [])

        if ("template_data" in data.keys() and not template_data) or not isinstance(
            template_data, list
        ):
            raise serializers.ValidationError(
                {
                    "template_data": [
                        "This field must be a non-empty list of dictionaries with keys 'template_id' and 'date'.",
                        "Expected format: [{'template_id': <int>, 'week': <int>, 'date': '<dd/mm/yyyy hh:mm:ss>'}]",
                    ]
                }
            )

        for entry in template_data:
            if (
                not isinstance(entry, dict)
                or "template_id" not in entry
                or "date" not in entry
                or "week" not in entry
            ):
                raise serializers.ValidationError(
                    {
                        "template_data": [
                            "Each item must match the following schema:",
                            "Expected format: {'template_id': <int>, 'week': <int>, 'date': '<dd/mm/yyyy hh:mm:ss>'}",
                        ]
                    }
                )
        return super().to_internal_value(data)

    def validate(self, data):
        request = self.context["request"]
        errors = validate_incoming_data(
            self.initial_data,
            ["engagement_id", "template_data"],
            partial=self.partial,
        )

        engagement_id = data.pop("engagement_id", None)
        engagement = None
        if engagement_id:
            engagement = Engagement.objects.filter(
                organization=request.user.clientuser.organization,
                pk=engagement_id,
            ).first()
            if not engagement:
                errors.setdefault("engagement_id", []).append("Invalid engagement_id")
            data["engagement"] = engagement

        if engagement:
            notice_weeks = int(engagement.notice_period.split("-")[1]) / 7
            max_template_assign = notice_weeks * 2

            templates = data.pop("template_data", [])
            already_associated_operation = EngagementOperation.objects.filter(
                engagement=engagement
            ).count()

            if (
                len(templates) > max_template_assign
                or already_associated_operation > max_template_assign
            ):
                errors.setdefault("template_ids", []).append(
                    "Max {} templates can be assigned ".format(int(max_template_assign))
                )

            week_count = {}
            for template in templates:
                week = template.get("week")
                if week is not None:
                    week_count[week] = week_count.get(week, 0) + 1

            if len(week_count) > notice_weeks:
                errors.setdefault("template_data", []).append(
                    "Number of weeks with templates assigned exceeds the notice period weeks."
                )

            if any(count > 2 for count in week_count.values()):
                errors.setdefault("template_data", []).append(
                    "Max 2 templates can be assigned per week."
                )

            invalid_dates = [
                template["date"].strftime("%d/%m/%Y %H:%M:%S")
                for template in templates
                if datetime.strptime(
                    template["date"].strftime("%d-%m-%Y %H:%M:%S"),
                    "%d-%m-%Y %H:%M:%S",
                )
                < datetime.strptime(
                    datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
                    "%d-%m-%Y %H:%M:%S",
                )
            ]

            if invalid_dates:
                errors.setdefault("template_data", []).append(
                    "Invalid dates present: {}. Dates should not be in past".format(
                        ", ".join(invalid_dates)
                    )
                )

            if not errors:
                valid_template_ids = set(
                    EngagementTemplates.objects.filter(
                        organization=request.user.clientuser.organization,
                        pk__in=[template["template_id"] for template in templates],
                    ).values_list("id", flat=True)
                )

                existing_template_ids = set(
                    EngagementOperation.objects.filter(
                        engagement=engagement, template_id__in=valid_template_ids
                    ).values_list("template_id", flat=True)
                )
                if existing_template_ids:
                    errors.setdefault("template_id", []).extend(
                        [
                            "Template id already exists for the given engagement: {}".format(
                                template_id
                            )
                            for template_id in existing_template_ids
                        ]
                    )

                invalid_template_ids = (
                    set(template["template_id"] for template in templates)
                    - valid_template_ids
                )

                if invalid_template_ids:
                    errors.setdefault("template_id", []).append(
                        "Invalid template_id: {}".format(
                            ", ".join(map(str, invalid_template_ids))
                        )
                    )

                data["templates"] = [
                    template
                    for template in templates
                    if template["template_id"] in valid_template_ids
                    and template["template_id"] not in existing_template_ids
                ]

        if errors:
            raise serializers.ValidationError({"errors": errors})

        return data

    def create(self, validated_data):
        engagement = validated_data["engagement"]
        templates = validated_data.pop("templates", [])

        operations = [
            EngagementOperation(
                engagement=engagement,
                template_id=template["template_id"],
                date=template["date"],
                week=template["week"],
            )
            for template in templates
        ]

        operations = EngagementOperation.objects.bulk_create(operations)

        task_group = group(
            send_schedule_engagement_email.s(operation.id).set(eta=operation.date)
            for operation in operations
        )
        result = task_group.apply_async()

        for operation, task in zip(operations, result.children):
            operation.task_id = task.id
            operation.save()

        return operations


class EngagementTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = EngagementTemplates
        fields = (
            "id",
            "template_name",
            "subject",
            "template_html_content",
            "attachment",
        )

    def validate(self, data):
        attachment = self.context.get("attachment")
        required_keys = ["template_name", "subject", "template_html_content"]
        allowed_keys = ["attachment"]
        errors = validate_incoming_data(
            self.initial_data,
            required_keys,
            allowed_keys,
            partial=self.partial,
            original_data=data,
            form=True,
        )
        if attachment:
            errors.update(
                validate_attachment(
                    "attachment",
                    data.get("attachment"),
                    [
                        "pdf",
                        "doc",
                        "docx",
                        "xls",
                        "xlsx",
                        "ppt",
                        "txt",
                        "pptx",
                        "jpeg",
                        "jpg",
                        "mp3",
                        "mp4",
                        "mkv",
                        "zip",
                    ],
                    25,
                )
            )
        if errors:
            raise serializers.ValidationError({"errors": errors})
        return data


class EngagementCandidateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Candidate
        fields = ("name", "phone", "email", "company", "cv")


class EngagementJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = Job
        fields = ("id", "name")


class EngagementClientUserSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = ClientUser
        fields = ("id", "name", "email")
        read_only_fields = ("email",)


class EngagementSerializer(serializers.ModelSerializer):
    candidate = EngagementCandidateSerializer(read_only=True)
    candidate_id = serializers.IntegerField(required=False, write_only=True)
    offer_date = serializers.DateField(
        input_formats=["%d/%m/%Y"], format="%d/%m/%Y", required=False
    )
    engagementoperations = EngagementOperationSerializer(read_only=True, many=True)

    status = serializers.ChoiceField(
        choices=Engagement.STATUS_CHOICE,
        error_messages={
            "invalid_choice": f"This is an invalid choice. Valid choices are {', '.join([f'{key}({value})' for key, value in Engagement.STATUS_CHOICE])}"
        },
        required=False,
    )
    notice_period = serializers.ChoiceField(
        choices=Engagement.NOTICE_PERIOD_CHOICE,
        error_messages={
            "invalid_choice": f"This is an invalid choice. Valid choices are {', '.join([f'{key}({value})' for key, value in Engagement.NOTICE_PERIOD_CHOICE])}"
        },
        required=False,
    )

    class Meta:
        model = Engagement
        fields = (
            "id",
            "candidate_name",
            "candidate_email",
            "candidate_phone",
            "candidate_id",
            "job",
            "candidate",
            "status",
            "notice_period",
            "offered",
            "offer_date",
            "offer_accepted",
            "other_offer",
            "gtp_name",
            "gtp_email",
            "candidate_cv",
            "engagementoperations",
        )
        extra_kwargs = {
            "candidate_id": {"write_only": True},
        }

    def validate(self, data):
        request = self.context["request"]
        errors = {}

        required_keys = [
            "job",
            "gtp_name",
            "gtp_email",
            "notice_period",
            "offered",
            "offer_accepted",
            "other_offer",
        ]
        allowed_keys = [
            "status",
            "offer_date",
        ]

        if (
            data.get("candidate_name")
            or data.get("candidate_email")
            or data.get("candidate_phone")
        ):
            required_keys.extend(
                ["candidate_name", "candidate_email", "candidate_phone", "candidate_cv"]
            )
        elif data.get("candidate_id"):
            required_keys.append("candidate_id")
        else:
            errors.setdefault("missing_candidate_details", []).append(
                "Either candidate_id or candidate_email, candidate_name, candidate_phone, candidate_cv is required"
            )

        errors.update(
            validate_incoming_data(
                self.initial_data,
                required_keys=required_keys,
                allowed_keys=allowed_keys,
                partial=self.partial,
                original_data=data,
                form=True,
            )
        )

        candidate_id = data.pop("candidate_id", None)
        if candidate_id:
            candidate = Candidate.objects.filter(
                organization=request.user.clientuser.organization, pk=candidate_id
            ).first()
            if not candidate:
                errors.setdefault("candidate_id", []).append("Invalid candidate_id")
            data["candidate"] = candidate

        candidate_cv = data.get("candidate_cv")
        if candidate_cv:
            errors.update(
                validate_attachment(
                    "candidate_cv", candidate_cv, ["pdf", "doc", "docx"], 5
                )
            )

        if data.get("offered") and not data.get("offer_date"):
            errors.setdefault("offer_date", []).append(
                "Offer date is required if 'offered' is True."
            )

        if data.get("offer_accepted") and not data.get("offered"):
            errors.setdefault("offer_accepted", []).append(
                "Offer cannot be accepted if it was never made."
            )

        if errors:
            raise serializers.ValidationError({"errors": errors})

        return data


class EngagementUpdateStatusSerializer(serializers.ModelSerializer):
    status = serializers.ChoiceField(
        choices=Engagement.STATUS_CHOICE,
        error_messages={
            "invalid_choice": f"This is an invalid choice. Valid choices are {', '.join([f'{key}({value})' for key, value in Engagement.STATUS_CHOICE])}"
        },
        required=False,
    )

    class Meta:
        model = Engagement
        fields = ("status",)


class EngagmentOperationStatusUpdateSerializer(serializers.ModelSerializer):
    status = serializers.ChoiceField(
        source="operation_complete_status",
        choices=EngagementOperation.DELIVERY_STATUS_CHOICES,
        error_messages={
            "invalid_choice": f"This is an invalid choice. Valid choices are {', '.join([f'{key}({value})' for key, value in EngagementOperation.DELIVERY_STATUS_CHOICES])}"
        },
    )

    class Meta:
        model = EngagementOperation
        fields = ("status",)

    def validate(self, data):
        errors = {}
        engagement_operation = self.instance

        if engagement_operation.delivery_status != "SUC":
            errors.setdefault("status", []).append(
                "Invalid status update request. As operation is not successfully delievered yet."
            )

        if errors:
            raise serializers.ValidationError({"errors": errors})
        return data


class FinanceCandidateSerializer(serializers.ModelSerializer):
    role = serializers.CharField(source="designation.name")

    class Meta:
        model = Candidate
        fields = ("name", "year", "month", "role")


class FinanceSerializer(serializers.ModelSerializer):
    candidate = FinanceCandidateSerializer(source="interview.candidate", read_only=True)
    scheduled_time = serializers.DateTimeField(
        source="interview.scheduled_time", format="%d/%m/%Y %H:%M:%S"
    )
    amount = serializers.DecimalField(
        source="amount_for_client", max_digits=10, decimal_places=2
    )

    class Meta:
        model = BillingLog
        fields = ("candidate", "scheduled_time", "amount")


class FinanceSerializerForInterviewer(serializers.ModelSerializer):
    candidate = FinanceCandidateSerializer(source="interview.candidate", read_only=True)
    scheduled_time = serializers.DateTimeField(
        source="interview.scheduled_time", format="%d/%m/%Y %H:%M:%S"
    )
    amount = serializers.DecimalField(
        source="amount_for_interviewer", max_digits=10, decimal_places=2
    )

    class Meta:
        model = BillingLog
        fields = ("candidate", "scheduled_time", "amount")


class AnalyticsQuerySerializer(serializers.Serializer):
    from_date = serializers.DateField(required=False, input_formats=["%d/%m/%Y"])
    to_date = serializers.DateField(required=False, input_formats=["%d/%m/%Y"])
    organization_id = serializers.IntegerField(required=False)

    def validate(self, data):
        from_date = data.get("from_date")
        to_date = data.get("to_date")
        errors = {}

        if not from_date or not to_date:
            errors["date"] = "Both 'from_date' and 'to_date' must be provided together."

        today = date.today()
        if from_date and to_date and (from_date > today or to_date > today):
            errors["date"] = "Dates cannot be in the future."

        if errors:
            raise serializers.ValidationError(errors)

        return data


class FeedbackPDFVideoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Interview
        fields = ("id", "recording")
