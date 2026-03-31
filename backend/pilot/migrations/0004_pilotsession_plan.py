from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pilot", "0003_pilotsession_chat_history"),
    ]

    operations = [
        migrations.AddField(
            model_name="pilotsession",
            name="plan",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
