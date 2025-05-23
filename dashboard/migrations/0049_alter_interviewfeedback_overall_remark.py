# Generated by Django 5.1.2 on 2025-03-19 10:29

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0048_interviewfeedback'),
    ]

    operations = [
        migrations.AlterField(
            model_name='interviewfeedback',
            name='overall_remark',
            field=models.CharField(choices=[('HREC', 'Highly Recommended'), ('REC', 'Recommended'), ('NREC', 'Not Recommended'), ('SNREC', 'Strongly Not Recommended'), ('NJ', 'Not Joined')], max_length=10),
        ),
    ]
