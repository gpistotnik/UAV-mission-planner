from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("missions", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="testflightsettings",
            name="hop_altitude_m",
            field=models.FloatField(default=2.0, verbose_name="Test 2 višina [m]"),
        ),
        migrations.AddField(
            model_name="testflightsettings",
            name="hop_countdown_s",
            field=models.FloatField(
                default=5.0, verbose_name="Test 2 odštevanje [s]"),
        ),
        migrations.AddField(
            model_name="testflightsettings",
            name="hop_leg_m",
            field=models.FloatField(default=2.0, verbose_name="Test 2 odmik [m]"),
        ),
        migrations.AddField(
            model_name="testflightsettings",
            name="hop_max_hdop",
            field=models.FloatField(
                default=1.2, verbose_name="Test 2 največji HDOP"),
        ),
        migrations.AddField(
            model_name="testflightsettings",
            name="hop_min_satellites",
            field=models.PositiveSmallIntegerField(
                default=12, verbose_name="Test 2 najmanj satelitov"),
        ),
        migrations.AddField(
            model_name="testflightsettings",
            name="hop_require_gps",
            field=models.BooleanField(
                default=True, verbose_name="Test 2 zahtevaj GPS"),
        ),
        migrations.AlterField(
            model_name="testflightsettings",
            name="altitude_m",
            field=models.FloatField(default=3.0, verbose_name="Test 1 višina [m]"),
        ),
        migrations.AlterField(
            model_name="testflightsettings",
            name="countdown_s",
            field=models.FloatField(
                default=5.0, verbose_name="Test 1 odštevanje [s]"),
        ),
        migrations.AlterField(
            model_name="testflightsettings",
            name="hover_s",
            field=models.FloatField(
                default=5.0, verbose_name="Test 1 lebdenje [s]"),
        ),
        migrations.AlterField(
            model_name="testflightsettings",
            name="max_hdop",
            field=models.FloatField(
                default=1.5, verbose_name="Test 1 največji HDOP"),
        ),
        migrations.AlterField(
            model_name="testflightsettings",
            name="min_satellites",
            field=models.PositiveSmallIntegerField(
                default=10, verbose_name="Test 1 najmanj satelitov"),
        ),
        migrations.AlterField(
            model_name="testflightsettings",
            name="require_gps",
            field=models.BooleanField(
                default=True, verbose_name="Test 1 zahtevaj GPS"),
        ),
    ]
