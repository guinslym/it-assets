# Generated by Django 2.0.4 on 2018-05-03 02:06

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('webconfig', '0001_initial'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='fqdn',
            options={'verbose_name': 'FQDN', 'verbose_name_plural': 'FQDNs'},
        ),
    ]