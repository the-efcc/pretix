import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        # Any existing pretixbase node works here: depending *into* pretixbase
        # does not create a leaf there. We deliberately point at the last
        # pristine-upstream migration rather than at one of our own, so this
        # stays valid even if we later retire our pretixbase migrations.
        ('pretixbase', '0306_alter_eventmetaproperty_unique_together'),
    ]

    operations = [
        migrations.CreateModel(
            name='InstallmentPlan',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('payment_provider', models.CharField(max_length=255)),
                ('payment_token', models.JSONField()),
                ('grace_warning_sent', models.BooleanField(default=False)),
                ('total_installments', models.PositiveIntegerField()),
                ('installments_paid', models.PositiveIntegerField(default=0)),
                ('amount_per_installment', models.DecimalField(decimal_places=2, max_digits=13)),
                ('status', models.CharField(default='active', max_length=20)),
                ('grace_period_end', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('order', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='installment_plan', to='pretixbase.order')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='ScheduledInstallment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('installment_number', models.PositiveIntegerField()),
                ('amount', models.DecimalField(decimal_places=2, max_digits=13)),
                ('due_date', models.DateTimeField()),
                ('state', models.CharField(db_index=True, default='pending', max_length=20)),
                ('processed_at', models.DateTimeField(blank=True, null=True)),
                ('failure_reason', models.TextField(null=True)),
                ('reminder_sent', models.BooleanField(default=False)),
                ('payment', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='installment', to='pretixbase.orderpayment')),
                ('plan', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='installments', to='efcc.installmentplan')),
            ],
            options={
                'ordering': ('installment_number',),
                'indexes': [models.Index(fields=['due_date', 'state'], name='efcc_schedu_due_dat_9f3615_idx')],
                'unique_together': {('plan', 'installment_number')},
            },
        ),
    ]
