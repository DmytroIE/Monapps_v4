# Generated by Django 5.2 on 2025-06-19 16:58

import os
import django.db.models.deletion
from django.db import migrations, models


additional_operations = []
if os.environ.get("SQL_ENGINE") == "django.db.backends.postgresql":
    additional_operations = [
        migrations.RunSQL(
            """
            SELECT create_hypertable('df_readings', 'time');
            """,
            reverse_sql="""
                DROP TABLE df_readings;
            """,
        ),
    ]


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("datafeeds", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="DfReading",
            fields=[
                (
                    "pk",
                    models.CompositePrimaryKey(
                        "datafeed_id", "time", blank=True, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("time", models.BigIntegerField()),
                ("db_value", models.FloatField()),
                ("restored", models.BooleanField(default=False)),
                ("datafeed", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="datafeeds.datafeed")),
            ],
            options={
                "db_table": "df_readings",
            },
        ),
        *additional_operations,
    ]
