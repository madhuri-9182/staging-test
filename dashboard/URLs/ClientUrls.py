from django.urls import path
from ..views import (
    ClientUserView,
    ClientInvitationActivateView,
    JobView,
    ResumeParserView,
    CandidateView,
    PotentialInterviewerAvailabilityForCandidateView,
    EngagementTemplateView,
    EngagementView,
    EngagementOperationView,
    EngagementOperationUpdateView,
    EngagementOperationStatusUpdateView,
    ClientDashboardView,
    FinanceView,
    CandidateAnalysisView,
    FeedbackPDFVideoView,
    BillPaymentView,
    CFWebhookView,
    PaymentStatusView,
)


urlpatterns = [
    path("client-user/", ClientUserView.as_view(), name="client-user"),
    path(
        "client-user/<int:client_user_id>/",
        ClientUserView.as_view(),
        name="client-user-details",
    ),
    path(
        "client-user-activation/<str:uid>/",
        ClientInvitationActivateView.as_view(),
        name="client-user-activation",
    ),
    path("candidates/", CandidateView.as_view(), name="candidates"),
    path("candidate/<int:candidate_id>/", CandidateView.as_view(), name="candidate"),
    path("jobs/", JobView.as_view(), name="job-list"),
    path("job/<int:job_id>/", JobView.as_view(), name="job-details"),
    path(
        "interviewer-availability/",
        PotentialInterviewerAvailabilityForCandidateView.as_view(),
        name="interviewer-availablity",
    ),
    path("parse-resume/", ResumeParserView.as_view(), name="resume-parser"),
    path(
        "engagement-templates/",
        EngagementTemplateView.as_view(),
        name="engagement-tempates",
    ),
    path(
        "engagement-template/<int:pk>/",
        EngagementTemplateView.as_view(),
        name="engagement-tempates",
    ),
    path("engagements/", EngagementView.as_view(), name="candidate-engagements"),
    path(
        "engagements/<int:engagement_id>/",
        EngagementView.as_view(),
        name="candidate-engagements-details",
    ),
    path(
        "engagement-operation/",
        EngagementOperationView.as_view(),
        name="engagement-operation",
    ),
    path(
        "engagement-operation/<int:engagement_id>/",
        EngagementOperationUpdateView.as_view(),
        name="engagement-operation-update",
    ),
    path(
        "engagement-operation-status-update/<int:engagement_operation_id>/",
        EngagementOperationStatusUpdateView.as_view(),
        name="engagement-operation-status-update",
    ),
    path(
        "dashboard/",
        ClientDashboardView.as_view(),
        name="client-dashboard",
    ),
    path("finance/", FinanceView.as_view(), name="client-finance"),
    path(
        "candidate-analysis/<int:job_id>/",
        CandidateAnalysisView.as_view(),
        name="candidate-analysis",
    ),
    path(
        "feedback-pdf-video/<str:interview_uid>/",
        FeedbackPDFVideoView.as_view(),
        name="feedback-pdf-video",
    ),
    path(
        "billpay/<str:billing_record_uid>/", BillPaymentView.as_view(), name="billpay"
    ),
    path("cashfree-webhook/", CFWebhookView.as_view(), name="cashfree-webhook"),
    path(
        "payment-status/<str:payment_link_id>/",
        PaymentStatusView.as_view(),
        name="payment-status",
    ),
]
