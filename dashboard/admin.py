from typing import Any
from django.contrib import admin

# Register your models here.
from django.contrib import admin
from django.db.models.query import QuerySet
from django.http import HttpRequest
from .models import (
    InternalClient,
    ClientPointOfContact,
    Job,
    ClientUser,
    EngagementTemplates,
    Candidate,
    InternalInterviewer,
    Interview,
    InterviewerAvailability,
    InterviewFeedback,
    BillingRecord,
    BillingLog,
    BillPayments,
)


@admin.register(Interview)
class InterviewAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "get_candidate_name",
        "get_interviewer_name",
        "get_organization_name",
        "created_at",
        "scheduled_time",
        "status",
    )
    list_filter = (
        "interviewer__name",
        "candidate__organization__internal_client__name",
    )
    search_fields = (
        "candidate__name",
        "interviewer__name",
        "candidate__organization__internal_client__name",
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related(
            "candidate",
            "candidate__organization",
            "candidate__organization__internal_client",
            "interviewer",
        )

    def get_candidate_name(self, obj):
        return obj.candidate.name if hasattr(obj.candidate, "name") else None

    get_candidate_name.short_description = "Candidate"

    def get_interviewer_name(self, obj):
        return obj.interviewer.name if hasattr(obj.interviewer, "name") else None

    get_interviewer_name.short_description = "Interviewer"

    def get_organization_name(self, obj):
        return obj.candidate.organization.internal_client.name

    get_organization_name.short_description = "Organization"


@admin.register(InternalInterviewer)
class InternalInterviewer(admin.ModelAdmin):
    list_display = (
        "name",
        "email",
        "phone_number",
        "total_experience_years",
        "total_experience_months",
    )
    search_fields = ("name", "email", "phone_number")
    list_filter = ("strength",)


@admin.register(InternalClient)
class InternalClientAdmin(admin.ModelAdmin):
    list_display = ("name", "gstin", "pan", "is_signed", "assigned_to")
    search_fields = ("name", "gstin", "pan")
    list_filter = ("is_signed",)


@admin.register(ClientPointOfContact)
class ClientPointOfContactAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "phone", "client")
    search_fields = ("name", "email")

    def get_queryset(self, request):
        return ClientPointOfContact.object_all.all()


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("pk", "name", "job_id")
    search_fields = ("name", "job_id")

    def get_queryset(self, request):
        return Job.object_all.all()


@admin.register(ClientUser)
class ClientUserAdmin(admin.ModelAdmin):
    list_display = ("id", "organization", "user", "name", "invited_by", "status")
    search_fields = ("id", "organization", "name")
    readonly_fields = ["created_at", "updated_at"]

    def get_queryset(self, request):
        return ClientUser.object_all.all()


@admin.register(EngagementTemplates)
class EnagagementTeamplteAdmin(admin.ModelAdmin):
    list_display = ("id", "template_name", "organization__name")
    search_fields = ("organization__name", "template_name")
    readonly_fields = ["created_at", "updated_at"]

    def get_queryset(self, request: HttpRequest) -> QuerySet[Any]:
        return EngagementTemplates.object_all.select_related("organization")


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "specialization", "organization__name")
    search_fields = ("organization__name",)
    readonly_fields = ["created_at", "updated_at"]


admin.site.register(InterviewerAvailability)


@admin.register(InterviewFeedback)
class InterviewFeedbackAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "get_interview_name",
        "overall_remark",
        "overall_score",
        "is_submitted",
    )
    list_filter = ("is_submitted",)
    search_fields = ("interview__candidate__name", "interview__interviewer__name")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related(
            "interview", "interview__candidate", "interview__interviewer"
        )

    def get_interview_name(self, obj):
        if not obj.interview:
            return "None"
        return f"{obj.interview.candidate.name} - {obj.interview.interviewer.name}"

    get_interview_name.short_description = "Interview"


@admin.register(BillingRecord)
class BillingRecordAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "amount_due",
        "due_date",
        "get_client_name",
        "get_interviewer_name",
        "created_at",
        "billing_month",
    )
    list_filter = ("client", "interviewer")
    search_fields = ("client__name", "interviewer__name")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("client", "interviewer")

    def get_client_name(self, obj):
        return obj.client.name if hasattr(obj.client, "name") else None

    get_client_name.short_description = "Client"

    def get_interviewer_name(self, obj):
        return obj.interviewer.name if hasattr(obj.interviewer, "name") else None

    get_interviewer_name.short_description = "Interviewer"


@admin.register(BillingLog)
class BillingLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "get_interview_name",
        "get_client_name",
        "get_interviewer_name",
        "amount_for_client",
        "amount_for_interviewer",
        "reason",
        "billing_month",
        "is_billing_calculated",
    )
    list_filter = ("reason", "billing_month", "is_billing_calculated")
    search_fields = (
        "interview__candidate__name",
        "client__name",
        "interviewer__name",
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("interview", "client", "interviewer")

    def get_interview_name(self, obj):
        return (
            f"{obj.interview.candidate.name} - {obj.interview.interviewer.name}"
            if obj.interview
            else "None"
        )

    get_interview_name.short_description = "Interview"

    def get_client_name(self, obj):
        return obj.client.name if obj.client else "None"

    get_client_name.short_description = "Client"

    def get_interviewer_name(self, obj):
        return obj.interviewer.name if obj.interviewer else "None"

    get_interviewer_name.short_description = "Interviewer"


@admin.register(BillPayments)
class BillPaymentsAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "get_billing_record",
        "amount",
        "payment_link_id",
        "payment_status",
        "payment_date",
        "transaction_id",
        "link_expired_time",
        "cf_link_id",
        "order_id",
        "customer_name",
        "customer_email",
    )
    list_filter = ("payment_status", "payment_date")
    search_fields = (
        "billing_record__invoice_number",
        "payment_link_id",
        "transaction_id",
        "cf_link_id",
        "order_id",
        "customer_name",
        "customer_phone",
        "customer_email",
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("billing_record")

    def get_billing_record(self, obj):
        return (
            getattr(obj.billing_record, "invoice_number", "Invoice Not Generated")
            if obj.billing_record
            else "None"
        )

    get_billing_record.short_description = "Billing Record"
