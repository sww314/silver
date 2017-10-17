from itertools import chain

from celery import group, shared_task
from celery_once import QueueOnce
from django.conf import settings
from django.utils import timezone
from redis.exceptions import LockError

from silver.documents_generator import DocumentsGenerator
from silver.models import Invoice, Proforma, Transaction
from silver.payment_processors.mixins import PaymentProcessorTypes
from silver.vendors.redis_server import redis


PDF_GENERATION_TIME_LIMIT = getattr(settings, 'PDF_GENERATION_TIME_LIMIT',
                                    60)  # default 60s


@shared_task(base=QueueOnce, once={'graceful': True},
             time_limit=PDF_GENERATION_TIME_LIMIT)
def generate_pdf(document_id, document_type):
    if document_type == 'Invoice':
        document = Invoice.objects.get(id=document_id)
    else:
        document = Proforma.objects.get(id=document_id)

    document.generate_pdf()


@shared_task(ignore_result=True)
def generate_pdfs():
    dirty_documents = chain(Invoice.objects.filter(pdf__dirty__gt=0),
                            Proforma.objects.filter(pdf__dirty__gt=0))

    # Generate PDFs in parallel
    group(generate_pdf.s(document.id, document.kind)
          for document in dirty_documents)()


DOCS_GENERATION_TIME_LIMIT = getattr(settings, 'DOCS_GENERATION_TIME_LIMIT',
                                     60 * 60)  # default 60m


@shared_task(base=QueueOnce, once={'graceful': True},
             time_limit=DOCS_GENERATION_TIME_LIMIT, ignore_result=True)
def generate_billing_documents(billing_date=None):
    if not billing_date:
        billing_date = timezone.now().date()

    DocumentsGenerator().generate(billing_date=billing_date)


FETCH_TRANSACTION_STATUS_TIME_LIMIT = getattr(settings, 'FETCH_TRANSACTION_STATUS_TIME_LIMIT',
                                              60)  # default 60s


@shared_task(base=QueueOnce, once={'graceful': True},
             time_limit=FETCH_TRANSACTION_STATUS_TIME_LIMIT)
def fetch_transaction_status(transaction_id):
    transaction = Transaction.objects.filter(pk=transaction_id).first()
    if not transaction:
        return

    payment_processor = transaction.payment_method.get_payment_processor()
    if payment_processor.type != PaymentProcessorTypes.Triggered:
        return

    payment_processor.fetch_transaction_status(transaction)


@shared_task(ignore_result=True)
def fetch_transactions_status(transaction_ids=None):
    eligible_transactions = Transaction.objects.filter(state=Transaction.States.Pending)

    if transaction_ids:
        eligible_transactions = eligible_transactions.filter(pk__in=transaction_ids)

    group(fetch_transaction_status.s(transaction.id) for transaction in eligible_transactions)()


EXECUTE_TRANSACTION_TIME_LIMIT = getattr(settings, 'EXECUTE_TRANSACTION_TIME_LIMIT',
                                         60)  # default 60s


@shared_task(base=QueueOnce, once={'graceful': True},
             time_limit=EXECUTE_TRANSACTION_TIME_LIMIT)
def execute_transaction(transaction_id):
    transaction = Transaction.objects.filter(pk=transaction_id).first()
    if not transaction:
        return

    if not transaction.payment_method.verified or transaction.payment_method.canceled:
        return

    payment_processor = transaction.payment_method.get_payment_processor()
    if payment_processor.type != PaymentProcessorTypes.Triggered:
        return

    payment_processor.execute_transaction(transaction)


@shared_task(ignore_result=True)
def execute_transactions(transaction_ids=None):
    executable_transactions = Transaction.objects.filter(state=Transaction.States.Initial)

    if transaction_ids:
        executable_transactions = executable_transactions.filter(pk__in=transaction_ids)

    group(execute_transaction.s(transaction.id) for transaction in executable_transactions)()
