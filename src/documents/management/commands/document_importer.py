import json
import logging
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

import tqdm
from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.core.serializers.base import DeserializationError
from django.db.models.signals import m2m_changed
from django.db.models.signals import post_save
from documents.models import Document
from documents.parsers import run_convert
from documents.settings import EXPORTER_ARCHIVE_NAME
from documents.settings import EXPORTER_FILE_NAME
from documents.settings import EXPORTER_THUMBNAIL_NAME
from filelock import FileLock
from paperless import version

from ...file_handling import create_source_path_directory
from ...signals.handlers import update_filename_and_move_files


@contextmanager
def disable_signal(sig, receiver, sender):
    try:
        sig.disconnect(receiver=receiver, sender=sender)
        yield
    finally:
        sig.connect(receiver=receiver, sender=sender)


class Command(BaseCommand):

    help = """
        Using a manifest.json file, load the data from there, and import the
        documents it refers to.
    """.replace(
        "    ",
        "",
    )

    def add_arguments(self, parser):
        parser.add_argument("source")
        parser.add_argument(
            "--no-progress-bar",
            default=False,
            action="store_true",
            help="If set, the progress bar will not be shown",
        )

    def __init__(self, *args, **kwargs):
        BaseCommand.__init__(self, *args, **kwargs)
        self.source = None
        self.manifest = None
        self.version = None

    def handle(self, *args, **options):

        logging.getLogger().handlers[0].level = logging.ERROR

        self.source = options["source"]

        if not os.path.exists(self.source):
            raise CommandError("That path doesn't exist")

        if not os.access(self.source, os.R_OK):
            raise CommandError("That path doesn't appear to be readable")

        manifest_paths = []

        main_manifest_path = os.path.normpath(
            os.path.join(self.source, "manifest.json"),
        )
        self._check_manifest_exists(main_manifest_path)

        with open(main_manifest_path) as f:
            self.manifest = json.load(f)
        manifest_paths.append(main_manifest_path)

        for root, dirs, files in os.walk(self.source):
            for file in files:
                if file.endswith("-manifest.json"):
                    doc_manifest_path = os.path.normpath(os.path.join(root, file))
                    with open(doc_manifest_path) as f:
                        self.manifest += json.load(f)
                    manifest_paths.append(doc_manifest_path)

        version_path = os.path.normpath(os.path.join(self.source, "version.json"))
        if os.path.exists(version_path):
            with open(version_path) as f:
                self.version = json.load(f)["version"]
                # Provide an initial warning if needed to the user
                if self.version != version.__full_version_str__:
                    self.stdout.write(
                        self.style.WARNING(
                            "Version mismatch: "
                            f"Currently {version.__full_version_str__},"
                            f" importing {self.version}."
                            " Continuing, but import may fail.",
                        ),
                    )

        else:
            self.stdout.write(self.style.NOTICE("No version.json file located"))

        self._check_manifest()
        with disable_signal(
            post_save,
            receiver=update_filename_and_move_files,
            sender=Document,
        ):
            with disable_signal(
                m2m_changed,
                receiver=update_filename_and_move_files,
                sender=Document.tags.through,
            ):
                # Fill up the database with whatever is in the manifest
                try:
                    for manifest_path in manifest_paths:
                        call_command("loaddata", manifest_path)
                except (FieldDoesNotExist, DeserializationError) as e:
                    self.stdout.write(self.style.ERROR("Database import failed"))
                    if (
                        self.version is not None
                        and self.version != version.__full_version_str__
                    ):
                        self.stdout.write(
                            self.style.ERROR(
                                "Version mismatch: "
                                f"Currently {version.__full_version_str__},"
                                f" importing {self.version}",
                            ),
                        )
                        raise e
                    else:
                        self.stdout.write(
                            self.style.ERROR("No version information present"),
                        )
                        raise e

                self._import_files_from_manifest(options["no_progress_bar"])

        self.stdout.write("Updating search index...")
        call_command(
            "document_index",
            "reindex",
            no_progress_bar=options["no_progress_bar"],
        )

    @staticmethod
    def _check_manifest_exists(path):
        if not os.path.exists(path):
            raise CommandError(
                "That directory doesn't appear to contain a manifest.json " "file.",
            )

    def _check_manifest(self):

        for record in self.manifest:

            if not record["model"] == "documents.document":
                continue

            if EXPORTER_FILE_NAME not in record:
                raise CommandError(
                    "The manifest file contains a record which does not "
                    "refer to an actual document file.",
                )

            doc_file = record[EXPORTER_FILE_NAME]
            if not os.path.exists(os.path.join(self.source, doc_file)):
                raise CommandError(
                    'The manifest file refers to "{}" which does not '
                    "appear to be in the source directory.".format(doc_file),
                )

            if EXPORTER_ARCHIVE_NAME in record:
                archive_file = record[EXPORTER_ARCHIVE_NAME]
                if not os.path.exists(os.path.join(self.source, archive_file)):
                    raise CommandError(
                        f"The manifest file refers to {archive_file} which "
                        f"does not appear to be in the source directory.",
                    )

    def _import_files_from_manifest(self, progress_bar_disable):

        os.makedirs(settings.ORIGINALS_DIR, exist_ok=True)
        os.makedirs(settings.THUMBNAIL_DIR, exist_ok=True)
        os.makedirs(settings.ARCHIVE_DIR, exist_ok=True)

        self.stdout.write("Copy files into paperless...")

        manifest_documents = list(
            filter(lambda r: r["model"] == "documents.document", self.manifest),
        )

        for record in tqdm.tqdm(manifest_documents, disable=progress_bar_disable):

            document = Document.objects.get(pk=record["pk"])

            doc_file = record[EXPORTER_FILE_NAME]
            document_path = os.path.join(self.source, doc_file)

            if EXPORTER_THUMBNAIL_NAME in record:
                thumb_file = record[EXPORTER_THUMBNAIL_NAME]
                thumbnail_path = Path(os.path.join(self.source, thumb_file)).resolve()
            else:
                thumbnail_path = None

            if EXPORTER_ARCHIVE_NAME in record:
                archive_file = record[EXPORTER_ARCHIVE_NAME]
                archive_path = os.path.join(self.source, archive_file)
            else:
                archive_path = None

            document.storage_type = Document.STORAGE_TYPE_UNENCRYPTED

            with FileLock(settings.MEDIA_LOCK):
                if os.path.isfile(document.source_path):
                    raise FileExistsError(document.source_path)

                create_source_path_directory(document.source_path)

                shutil.copy2(document_path, document.source_path)

                if thumbnail_path:
                    if thumbnail_path.suffix in {".png", ".PNG"}:
                        run_convert(
                            density=300,
                            scale="500x5000>",
                            alpha="remove",
                            strip=True,
                            trim=False,
                            auto_orient=True,
                            input_file=f"{thumbnail_path}[0]",
                            output_file=str(document.thumbnail_path),
                        )
                    else:
                        shutil.copy2(thumbnail_path, document.thumbnail_path)

                if archive_path:
                    create_source_path_directory(document.archive_path)
                    # TODO: this assumes that the export is valid and
                    #  archive_filename is present on all documents with
                    #  archived files
                    shutil.copy2(archive_path, document.archive_path)

            document.save()
