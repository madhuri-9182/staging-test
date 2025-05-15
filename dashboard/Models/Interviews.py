from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from .Client import Candidate
from .Internal import InternalInterviewer, Agreement, InterviewerPricing
from hiringdogbackend.ModelUtils import SoftDelete, CreateUpdateDateTimeAndArchivedField


class Interview(CreateUpdateDateTimeAndArchivedField):
    objects = SoftDelete()
    object_all = models.Manager()

    candidate = models.ForeignKey(
        Candidate, on_delete=models.DO_NOTHING, related_name="interviews", blank=True
    )
    interviewer = models.ForeignKey(
        InternalInterviewer,
        on_delete=models.DO_NOTHING,
        related_name="interviews",
        blank=True,
        null=True,
    )
    availability = models.ForeignKey(
        "InterviewerAvailability",
        on_delete=models.SET_NULL,
        related_name="interview_availablity",
        null=True,
        blank=True,
    )
    no_of_time_processed = models.IntegerField(
        default=0,
        help_text="Signifying the number of times that task is processed. if the task process more that 3 times then the interview doesn't happen.",
    )
    status = models.CharField(
        max_length=15,
        choices=Candidate.STATUS_CHOICES,
        blank=True,
        help_text="Interview status",
    )
    scheduled_time = models.DateTimeField(
        help_text="Scheduled interview date and time", null=True
    )
    previous_interview = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rescheduled_interviews",
        help_text="Reference to the previous interview instance if rescheduled.",
    )
    recording = models.FileField(
        upload_to="interview_recordings",
        blank=True,
        null=True,
        help_text="Interview recording file",
    )
    transcription = models.FileField(
        upload_to="interview_recordings_transcription",
        null=True,
        blank=True,
        help_text="recordings transcribe files",
    )
    downloaded = models.BooleanField(
        default=False, help_text="signifies that video is downloaded and stored"
    )
    feedback = models.TextField(
        blank=True, null=True, help_text="Feedback for the candidate"
    )
    score = models.PositiveSmallIntegerField(default=0)
    total_score = models.PositiveSmallIntegerField(default=0)
    scheduled_service_account_event_id = models.CharField(
        max_length=255, null=True, blank=True
    )
    meeting_link = models.URLField(null=True, blank=True)

    class Meta:
        unique_together = ("interviewer", "scheduled_time")

    def save(self, *args, **kwargs):
        candidate = self.candidate

        super().save(*args, **kwargs)
        candidate.status = self.status
        candidate.score = self.score
        candidate.total_score = self.total_score
        candidate.save()


class InterviewFeedback(CreateUpdateDateTimeAndArchivedField):
    interview = models.OneToOneField(
        Interview,
        on_delete=models.CASCADE,
        related_name="interview_feedback",
        help_text="The interviewer's feedback on interview.",
        blank=True,
        null=True,
    )
    skill_based_performance = models.JSONField(default=dict)
    skill_evaluation = models.JSONField(default=dict)
    strength = models.CharField(max_length=800, null=True)
    improvement_points = models.CharField(max_length=800, null=True)
    overall_remark = models.CharField(
        max_length=10,
        null=True,
        choices=(
            ("HREC", "Highly Recommended"),
            ("REC", "Recommended"),
            ("NREC", "Not Recommended"),
            ("SNREC", "Strongly Not Recommended"),
            ("NJ", "Not Joined"),
        ),
    )
    overall_score = models.PositiveSmallIntegerField(
        default=0,
        validators=[
            MinValueValidator(0, message="Score should be greater than or equal to 0"),
            MaxValueValidator(100, message="Scroe must not exceed 100"),
        ],
    )
    is_submitted = models.BooleanField(
        default=False,
        help_text="Signify whether the interviewer has submitted their feedback for this interview or not.",
    )
    pdf_file = models.FileField(upload_to="feedback_report", null=True, blank=True)
    attachment = models.FileField(
        upload_to="feedback_attachments", null=True, blank=True
    )
    link = models.URLField(
        null=True, blank=True, help_text="interview_answer_link_if_any"
    )

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

        if self.is_submitted and self.interview:
            interview = self.interview
            interview.status = self.overall_remark
            interview.score = self.overall_score
            interview.save(update_fields=["status", "score"])
