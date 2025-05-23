# Generated by Django 5.1.2 on 2025-04-07 13:45

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0062_internalclient_client_level_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='job',
            name='is_diversity_hiring',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='candidate',
            name='gender',
            field=models.CharField(blank=True, choices=[('M', 'Male'), ('F', 'Female'), ('TG', 'Transgender')], max_length=2, null=True),
        ),
    ]
