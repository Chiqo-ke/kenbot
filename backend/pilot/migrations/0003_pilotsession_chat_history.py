from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pilot', '0002_alter_pilotsession_user'),
    ]

    operations = [
        migrations.AddField(
            model_name='pilotsession',
            name='chat_history',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
