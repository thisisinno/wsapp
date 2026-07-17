from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("api", "0001_initial")]
    operations = [
        migrations.AddField(model_name="campaign", name="dispatch_task_id", field=models.CharField(blank=True, default="", max_length=255)),
        migrations.AddField(model_name="campaign", name="queue_error", field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="campaign", name="last_progress_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="campaign", name="last_enqueued_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="campaign", name="run_token", field=models.CharField(blank=True, default="", max_length=64)),
        migrations.AddField(model_name="campaignrecipient", name="sequence_number", field=models.PositiveIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="campaignrecipient", name="scheduled_for", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="campaignrecipient", name="attempt_started_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="campaignrecipient", name="attempt_finished_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AlterField(model_name="campaignrecipient", name="state", field=models.CharField(choices=[("invalid", "Invalid"), ("skipped", "Skipped"), ("cancelled", "Cancelled"), ("queued", "Queued"), ("processing", "Sending"), ("accepted", "Provider accepted"), ("pending", "Pending"), ("sent", "Sent"), ("delivered", "Delivered"), ("read", "Read"), ("played", "Played"), ("failed", "Failed")], default="queued", max_length=20)),
    ]
