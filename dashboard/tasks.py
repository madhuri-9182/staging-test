import os
import requests
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import F
from django.template.loader import render_to_string
from django.utils import timezone
from celery import shared_task, chain, group
from celery.exceptions import Reject
from django.conf import settings
from django.utils.safestring import mark_safe
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from .models import EngagementOperation, Interview, InterviewFeedback
from externals.google.google_meet import download_from_google_drive
from datetime import datetime, timedelta
from externals.feedback.interview_feedback import (
    analyze_transcription_and_generate_feedback,
)

CONTACT_EMAIL = settings.EMAIL_HOST_USER if settings.DEBUG else settings.CONTACT_EMAIL
INTERVIEW_EMAIL = (
    settings.EMAIL_HOST_USER if settings.DEBUG else settings.INTERVIEW_EMAIL
)


@shared_task(bind=True, max_retries=3, rate_limit="10/m")
def send_mail(
    self,
    to,
    subject,
    template,
    reply_to=CONTACT_EMAIL,
    attachments=[],
    bcc=None,
    **kwargs,
):
    email_type = kwargs.get("type")
    context = {
        "email": to,
        **kwargs,
    }

    try:
        content = render_to_string(template, context=context)
        email_message = EmailMultiAlternatives(
            subject,
            "",
            (
                INTERVIEW_EMAIL
                if email_type and email_type in ["feedback_notification"]
                else CONTACT_EMAIL
            ),
            [to],
            reply_to=[reply_to],
            bcc=[bcc],
        )
        email_message.attach_alternative(content, "text/html")
        for attachment in attachments:
            email_message.attach_file(attachment)
        email_message.send(fail_silently=True)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=60, retry_jitter=True)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=60, retry_jitter=True)
def send_email_to_multiple_recipients(
    self,
    contexts,
    subject,
    template,
    reply_to=CONTACT_EMAIL,
    attachments=[],
    bcc=None,
    **kwargs,
):
    emails = []

    with get_connection() as connection:
        for context in contexts:
            replies_to = [reply_to]
            email_address = context.get("email")
            from_email = context.get("from_email")
            recruiter_email = context.get("recruiter_email")

            if recruiter_email:
                replies_to.append(recruiter_email)

            if context.get("subject"):
                subject = context["subject"]

            if context.get("template"):
                template = context["template"]

            if not email_address:
                continue

            html_content = render_to_string(template, context)

            email = EmailMultiAlternatives(
                subject=subject,
                body="This is an HTML email. Please view it in an HTML-compatible email client.",
                from_email=from_email if from_email else CONTACT_EMAIL,
                to=[email_address],
                reply_to=replies_to,
                bcc=[bcc],
                connection=connection,
            )
            for attachment in attachments:
                email.attach_file(attachment)
            email.attach_alternative(html_content, "text/html")
            emails.append(email)

        if emails:
            connection.send_messages(emails)


@shared_task(bind=True, max_retries=4)
def send_schedule_engagement_email(self, engagement_operation_id):
    try:
        engagement_operation_obj = (
            EngagementOperation.objects.select_related(
                "template", "engagement", "engagement__candidate"
            )
            .only(
                "template__subject",
                "template__template_html_content",
                "engagement__candidate__email",
                "engagement__candidate_email",
            )
            .get(pk=engagement_operation_id)
        )

        email = EmailMultiAlternatives(
            subject=engagement_operation_obj.template.subject,
            body="This is an email.",
            from_email=CONTACT_EMAIL,
            to=[
                getattr(
                    engagement_operation_obj.engagement.candidate,
                    "email",
                    engagement_operation_obj.engagement.candidate_email,
                )
            ],
        )
        email.attach_alternative(
            mark_safe(engagement_operation_obj.template.template_html_content),
            "text/html",
        )
        email.send()
        engagement_operation_obj.delivery_status = "SUC"
        engagement_operation_obj.save()
    except Exception as e:
        engagement_operation_obj.delivery_status = "FLD"
        engagement_operation_obj.save()
        if self.request.revoked:
            raise Ignore()
        raise self.retry(exec=e, countdown=60)


@shared_task
def fetch_interview_records():
    current_time = timezone.now()
    before_one_and_half_an_hour = current_time - timedelta(hours=2, minutes=30)
    interview_qs = Interview.objects.filter(
        scheduled_time__lte=before_one_and_half_an_hour,
        status="CSCH",
        downloaded=False,
        scheduled_service_account_event_id__isnull=False,
        no_of_time_processed__lte=3,
    ).values_list("id", "scheduled_service_account_event_id")
    return list(interview_qs)


@shared_task(bind=True, retry_backoff=10, max_retries=3)
def download_recordings_from_google_drive(self, interview_info):
    if not interview_info or len(interview_info) != 2:
        raise Reject("Missing or invalid interview info")
    interview_id, event_id = interview_info
    try:
        download_recording_info = download_from_google_drive(interview_id, event_id)
        if not download_recording_info:
            Interview.objects.filter(pk=interview_id).update(
                no_of_time_processed=F("no_of_time_processed") + 1
            )
            raise Reject(f"Failed to download recordings for Interview {interview_id}")
        return download_recording_info
    except Reject:
        raise
    except Exception as e:
        print(
            f"Exception occured in download_recordings_from_google_drive:{interview_id} - {str(e)}"
        )
        raise self.retry(exc=e)


@shared_task
def store_recordings(recording_info):
    try:
        interview = Interview.objects.get(pk=recording_info["interview_id"])
    except Interview.DoesNotExist:
        raise Reject(f"Interview {recording_info['interview_id']} not found")
    files_to_delete = []
    with transaction.atomic():
        for file_type, file in recording_info["files"].items():
            try:
                with open(file["path"], "rb") as f:
                    if file_type == "video":
                        interview.recording.save(file["name"], f)
                    elif file_type == "transcript":
                        interview.transcription.save(file["name"], f)
            except Exception as e:
                raise Reject(f"Error processing file {file['path']}: {str(e)}")
            files_to_delete.append(file["path"])

        interview.downloaded = True
        interview.no_of_time_processed += 1
        interview.save(
            update_fields=[
                "recording",
                "transcription",
                "downloaded",
                "no_of_time_processed",
            ]
        )

    for file_path in files_to_delete:
        if os.path.exists(file_path):
            os.remove(file_path)

    return interview.id


@shared_task(bind=True)
def process_interview_recordings(self, interview_record_ids):
    if not interview_record_ids:
        raise Reject("No interviews to process")

    tasks = [
        chain(
            download_recordings_from_google_drive.s(interview_info),
            store_recordings.s(),
        )
        for interview_info in interview_record_ids
    ]
    group(*tasks).apply_async()


@shared_task
def trigger_interview_processing():
    chain(fetch_interview_records.s(), process_interview_recordings.s()).apply_async()


@shared_task(bind=True, retry_backoff=5, max_retries=3)
def process_interview_video_and_generate_and_store_feedback(self):
    interviews = (
        Interview.objects.filter(
            transcription__isnull=False, interview_feedback__isnull=True
        )
        .exclude(transcription="")
        .only("id", "feedback")
    )
    print(interviews)
    processed_ids = []
    for interview in interviews:
        try:
            with interview.transcription.open("r") as f:
                file_content = f.read()
            extracted_data = analyze_transcription_and_generate_feedback(file_content)

            InterviewFeedback.objects.update_or_create(
                interview_id=interview.id, defaults={**extracted_data}
            )
            processed_ids.append(interview.id)
            interviewer_name = interview.interviewer.name
            candidate_name = interview.candidate.name
            contexts = [
                {
                    "interviewer_name": interviewer_name,
                    "candidate_name": candidate_name,
                    "dashboard_link": f"https://{settings.SITE_DOMAIN}/",
                    "type": "feedback_notification",
                    "email": interview.interviewer.email,
                    "from_email": INTERVIEW_EMAIL,
                    "subject": f"Ready to Review? Feedback for {candidate_name} is Live",
                    "template": "interview_feedback_notification_email.html",
                },
                {
                    "internal_user_name": interview.candidate.organization.internal_client.assigned_to.name,
                    "organization_name": interview.candidate.organization.name,
                    "position": interview.candidate.designation.get_name_display(),
                    "interviewer_name": interview.interviewer.name,
                    "interview_date": interview.scheduled_time.strftime(
                        "%d/%m/%Y %H:%M"
                    ),
                    "candidate_name": candidate_name,
                    "email": interview.candidate.organization.internal_client.assigned_to.user.email,
                    "from_email": INTERVIEW_EMAIL,
                    "subject": f"Feedback Report Generated: Insights from {interviewer_name}'s Interview with {candidate_name}",
                    "template": "internal_interview_feedback_report_generated_conformation.html",
                },
            ]
            send_email_to_multiple_recipients.delay(contexts, "", "")
        except Exception as e:
            print(str(e))
    return f"Interview feedback created successfully for {processed_ids}."


@shared_task(bind=True, retry_backoff=5, max_retries=3)
def download_feedback_pdf(self, interview_uid):
    from dashboard.Serializers.InterviewerSerializers import InterviewFeedbackSerializer

    interview_feedback = (
        InterviewFeedback.objects.filter(interview_id=interview_uid)
        .select_related(
            "interview",
            "interview__candidate",
            "interview__candidate__designation",
            "interview__interviewer",
        )
        .first()
    )
    serializer = InterviewFeedbackSerializer(interview_feedback)
    interview_uid = urlsafe_base64_encode(
        force_bytes(f"interview_id:{interview_feedback.interview.id}")
    )
    data = serializer.data
    data["url"] = f"{interview_uid}"
    response = requests.post(
        "http://localhost:3000/generate-pdf", json=data, stream=True
    )
    if response.status_code == 200:
        candidate = interview_feedback.interview.candidate
        designation = candidate.designation.get_name_display()
        save_path = f"/tmp/{candidate.name}_{designation}_Feedback_Round 1_{timezone.now().strftime('%Y%m%d-%H%M%S')}.pdf"
        with open(save_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        with open(save_path, "rb") as f:
            interview_feedback.pdf_file.save(
                f"{candidate.name}_{designation}_Feedback_Round 1_{timezone.now().strftime('%Y%m%d-%H%M%S')}.pdf",
                f,
            )
        if os.path.exists(save_path):
            os.remove(save_path)
        return "Successfully Saved"
    else:
        error_message = response.content.decode("utf-8")
        print(f"Failed to generate PDF: {error_message}")
        self.retry(exc=Exception("Failed to generate PDF"))
