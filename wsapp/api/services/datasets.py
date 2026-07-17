from django.db import transaction

from api.models import ImportedRecipient, UploadedDataset
from api.services.imports import parse_tabular
from api.services.phones import normalize_tanzania_phone


def process_dataset(dataset_id, sheet_name="", header_row=1):
    dataset = UploadedDataset.objects.get(pk=dataset_id)
    dataset.processing_status = UploadedDataset.Status.PROCESSING
    dataset.processing_error = ""
    dataset.save(update_fields=["processing_status", "processing_error", "updated_at"])
    try:
        with dataset.original_file.open("rb") as source:
            columns, rows = parse_tabular(
                source, dataset.original_filename, sheet_name, header_row
            )
        with transaction.atomic():
            dataset.recipients.all().delete()
            ImportedRecipient.objects.bulk_create(
                [
                    ImportedRecipient(
                        dataset=dataset,
                        owner=dataset.owner,
                        original_row_number=number,
                        row_data=row,
                    )
                    for number, row in rows
                ],
                batch_size=1000,
            )
            dataset.detected_columns = columns
            dataset.row_count = len(rows)
            dataset.sheet_name = sheet_name
            dataset.header_row_number = header_row
            dataset.processing_status = UploadedDataset.Status.READY
            dataset.processing_error = ""
            dataset.save()
    except Exception as exc:
        dataset.processing_status = UploadedDataset.Status.FAILED
        dataset.processing_error = str(exc)[:1000]
        dataset.save(update_fields=["processing_status", "processing_error", "updated_at"])
    return dataset


def normalize_dataset(dataset_id, column):
    dataset = UploadedDataset.objects.get(pk=dataset_id)
    seen = {}
    suppressions = set(
        dataset.owner.suppressions.values_list("normalized_phone", flat=True)
    )
    with transaction.atomic():
        for recipient in dataset.recipients.order_by("original_row_number"):
            result = normalize_tanzania_phone(recipient.row_data.get(column, ""))
            recipient.phone_source_column = column
            recipient.phone_original = result.original
            recipient.phone_normalized = result.normalized
            recipient.phone_validation_status = result.status
            recipient.validation_error_code = result.code
            recipient.validation_error_message = result.message
            recipient.auto_corrected = result.auto_corrected
            recipient.suppressed = result.normalized in suppressions
            recipient.duplicate_of = seen.get(result.normalized) if result.normalized else None
            recipient.save()
            if result.normalized and result.normalized not in seen:
                seen[result.normalized] = recipient
        dataset.selected_phone_column = column
        dataset.save(update_fields=["selected_phone_column", "updated_at"])
    return dataset
