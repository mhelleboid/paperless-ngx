import hashlib
import json
import os
import shutil
import tempfile
import time

import tqdm
from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.auth.models import User
from django.core import serializers
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone
from documents.models import Comment
from documents.models import Correspondent
from documents.models import Document
from documents.models import DocumentType
from documents.models import SavedView
from documents.models import SavedViewFilterRule
from documents.models import StoragePath
from documents.models import Tag
from documents.models import UiSettings
from documents.settings import EXPORTER_ARCHIVE_NAME
from documents.settings import EXPORTER_FILE_NAME
from documents.settings import EXPORTER_THUMBNAIL_NAME
from filelock import FileLock
from paperless import version
from paperless.db import GnuPG
from paperless_mail.models import MailAccount
from paperless_mail.models import MailRule

from ...file_handling import delete_empty_directories
from ...file_handling import generate_filename


class Command(BaseCommand):

    help = """
        Decrypt and rename all files in our collection into a given target
        directory.  And include a manifest file containing document data for
        easy import.
    """.replace(
        "    ",
        "",
    )

    def add_arguments(self, parser):
        parser.add_argument("target")

        parser.add_argument(
            "-sm",
            "--split-manifest",
            default=False,
            action="store_true",
            help="Export document information in individual manifest json files.",
        )

        parser.add_argument(
            "-c",
            "--compare-checksums",
            default=False,
            action="store_true",
            help="Compare file checksums when determining whether to export "
            "a file or not. If not specified, file size and time "
            "modified is used instead.",
        )

        parser.add_argument(
            "-f",
            "--use-filename-format",
            default=False,
            action="store_true",
            help="Use PAPERLESS_FILENAME_FORMAT for storing files in the "
            "export directory, if configured.",
        )

        parser.add_argument(
            "-p",
            "--use-filename-prefix",
            default=False,
            action="store_true",
            help="Export files in dedicated folders according to their nature: "
            "archive, originals or thumbnails",
        )

        parser.add_argument(
            "-d",
            "--delete",
            default=False,
            action="store_true",
            help="After exporting, delete files in the export directory that "
            "do not belong to the current export, such as files from "
            "deleted documents.",
        )

        parser.add_argument(
            "-na",
            "--no-archive",
            default=False,
            action="store_true",
            help="Avoid exporting archive files",
        )

        parser.add_argument(
            "-nt",
            "--no-thumbnail",
            default=False,
            action="store_true",
            help="Avoid exporting thumbnail files",
        )

        parser.add_argument(
            "--no-progress-bar",
            default=False,
            action="store_true",
            help="If set, the progress bar will not be shown",
        )

        parser.add_argument(
            "-z",
            "--zip",
            default=False,
            action="store_true",
            help="Export the documents to a zip file in the given directory",
        )

    def __init__(self, *args, **kwargs):
        BaseCommand.__init__(self, *args, **kwargs)
        self.target = None
        self.split_manifest = None
        self.files_in_export_dir = []
        self.exported_files = []
        self.compare_checksums = False
        self.use_filename_format = False
        self.use_filename_prefix = False
        self.delete = False
        self.no_archive = False
        self.no_thumbnail = False

    def handle(self, *args, **options):

        self.target = options["target"]
        self.split_manifest = options["split_manifest"]
        self.compare_checksums = options["compare_checksums"]
        self.use_filename_format = options["use_filename_format"]
        self.use_filename_prefix = options["use_filename_prefix"]
        self.delete = options["delete"]
        self.no_archive = options["no_archive"]
        self.no_thumbnail = options["no_thumbnail"]
        zip_export: bool = options["zip"]

        # If zipping, save the original target for later and
        # get a temporary directory for the target
        temp_dir = None
        original_target = None
        if zip_export:
            original_target = self.target
            temp_dir = tempfile.TemporaryDirectory(
                dir=settings.SCRATCH_DIR,
                prefix="paperless-export",
            )
            self.target = temp_dir.name

        if not os.path.exists(self.target):
            raise CommandError("That path doesn't exist")

        if not os.access(self.target, os.W_OK):
            raise CommandError("That path doesn't appear to be writable")

        try:
            with FileLock(settings.MEDIA_LOCK):
                self.dump(options["no_progress_bar"])

                # We've written everything to the temporary directory in this case,
                # now make an archive in the original target, with all files stored
                if zip_export:
                    shutil.make_archive(
                        os.path.join(
                            original_target,
                            f"export-{timezone.localdate().isoformat()}",
                        ),
                        format="zip",
                        root_dir=temp_dir.name,
                    )

        finally:
            # Always cleanup the temporary directory, if one was created
            if zip_export and temp_dir is not None:
                temp_dir.cleanup()

    def dump(self, progress_bar_disable=False):
        # 1. Take a snapshot of what files exist in the current export folder
        for root, dirs, files in os.walk(self.target):
            self.files_in_export_dir.extend(
                map(lambda f: os.path.abspath(os.path.join(root, f)), files),
            )

        # 2. Create manifest, containing all correspondents, types, tags, storage paths
        # comments, documents and ui_settings
        with transaction.atomic():
            manifest = json.loads(
                serializers.serialize("json", Correspondent.objects.all()),
            )

            manifest += json.loads(serializers.serialize("json", Tag.objects.all()))

            manifest += json.loads(
                serializers.serialize("json", DocumentType.objects.all()),
            )

            manifest += json.loads(
                serializers.serialize("json", StoragePath.objects.all()),
            )

            manifest += json.loads(
                serializers.serialize("json", Comment.objects.all()),
            )

            documents = Document.objects.order_by("id")
            document_map = {d.pk: d for d in documents}
            document_manifest = json.loads(serializers.serialize("json", documents))
            if not self.split_manifest:
                manifest += document_manifest

            manifest += json.loads(
                serializers.serialize("json", MailAccount.objects.all()),
            )

            manifest += json.loads(
                serializers.serialize("json", MailRule.objects.all()),
            )

            manifest += json.loads(
                serializers.serialize("json", SavedView.objects.all()),
            )

            manifest += json.loads(
                serializers.serialize("json", SavedViewFilterRule.objects.all()),
            )

            manifest += json.loads(serializers.serialize("json", Group.objects.all()))

            manifest += json.loads(serializers.serialize("json", User.objects.all()))

            manifest += json.loads(
                serializers.serialize("json", UiSettings.objects.all()),
            )

        # 3. Export files from each document
        for index, document_dict in tqdm.tqdm(
            enumerate(document_manifest),
            total=len(document_manifest),
            disable=progress_bar_disable,
        ):
            # 3.1. store files unencrypted
            document_dict["fields"]["storage_type"] = Document.STORAGE_TYPE_UNENCRYPTED

            document = document_map[document_dict["pk"]]

            # 3.2. generate a unique filename
            filename_counter = 0
            while True:
                if self.use_filename_format:
                    base_name = generate_filename(
                        document,
                        counter=filename_counter,
                        append_gpg=False,
                    )
                else:
                    base_name = document.get_public_filename(counter=filename_counter)

                if base_name not in self.exported_files:
                    self.exported_files.append(base_name)
                    break
                else:
                    filename_counter += 1

            # 3.3. write filenames into manifest
            original_name = base_name
            if self.use_filename_prefix:
                original_name = os.path.join("originals", original_name)
            original_target = os.path.join(self.target, original_name)
            document_dict[EXPORTER_FILE_NAME] = original_name

            if not self.no_thumbnail:
                thumbnail_name = base_name + "-thumbnail.webp"
                if self.use_filename_prefix:
                    thumbnail_name = os.path.join("thumbnails", thumbnail_name)
                thumbnail_target = os.path.join(self.target, thumbnail_name)
                document_dict[EXPORTER_THUMBNAIL_NAME] = thumbnail_name
            else:
                thumbnail_target = None

            if not self.no_archive and document.has_archive_version:
                archive_name = base_name + "-archive.pdf"
                if self.use_filename_prefix:
                    archive_name = os.path.join("archive", archive_name)
                archive_target = os.path.join(self.target, archive_name)
                document_dict[EXPORTER_ARCHIVE_NAME] = archive_name
            else:
                archive_target = None

            # 3.4. write files to target folder
            t = int(time.mktime(document.created.timetuple()))
            if document.storage_type == Document.STORAGE_TYPE_GPG:

                os.makedirs(os.path.dirname(original_target), exist_ok=True)
                with open(original_target, "wb") as f:
                    with document.source_file as out_file:
                        f.write(GnuPG.decrypted(out_file))
                        os.utime(original_target, times=(t, t))

                if thumbnail_target:
                    os.makedirs(os.path.dirname(thumbnail_target), exist_ok=True)
                    with open(thumbnail_target, "wb") as f:
                        with document.thumbnail_file as out_file:
                            f.write(GnuPG.decrypted(out_file))
                            os.utime(thumbnail_target, times=(t, t))

                if archive_target:
                    os.makedirs(os.path.dirname(archive_target), exist_ok=True)
                    with open(archive_target, "wb") as f:
                        with document.archive_path as out_file:
                            f.write(GnuPG.decrypted(out_file))
                            os.utime(archive_target, times=(t, t))
            else:
                self.check_and_copy(
                    document.source_path,
                    document.checksum,
                    original_target,
                )

                if thumbnail_target:
                    self.check_and_copy(document.thumbnail_path, None, thumbnail_target)

                if archive_target:
                    self.check_and_copy(
                        document.archive_path,
                        document.archive_checksum,
                        archive_target,
                    )

            if self.split_manifest:
                manifest_name = base_name + "-manifest.json"
                if self.use_filename_prefix:
                    manifest_name = os.path.join("json", manifest_name)
                manifest_name = os.path.join(self.target, manifest_name)
                if manifest_name in self.files_in_export_dir:
                    self.files_in_export_dir.remove(manifest_name)
                os.makedirs(os.path.dirname(manifest_name), exist_ok=True)
                with open(manifest_name, "w") as f:
                    json.dump([document_manifest[index]], f, indent=2)

        # 4.1 write manifest to target folder
        manifest_path = os.path.abspath(os.path.join(self.target, "manifest.json"))
        if manifest_path in self.files_in_export_dir:
            self.files_in_export_dir.remove(manifest_path)

        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

        # 4.2 write version information to target folder
        version_path = os.path.abspath(os.path.join(self.target, "version.json"))
        if version_path in self.files_in_export_dir:
            self.files_in_export_dir.remove(version_path)

        with open(version_path, "w") as f:
            json.dump({"version": version.__full_version_str__}, f, indent=2)

        if self.delete:
            # 5. Remove files which we did not explicitly export in this run

            for f in self.files_in_export_dir:
                os.remove(f)

                delete_empty_directories(
                    os.path.abspath(os.path.dirname(f)),
                    os.path.abspath(self.target),
                )

    def check_and_copy(self, source, source_checksum, target):
        if os.path.abspath(target) in self.files_in_export_dir:
            self.files_in_export_dir.remove(os.path.abspath(target))

        perform_copy = False

        if os.path.exists(target):
            source_stat = os.stat(source)
            target_stat = os.stat(target)
            if self.compare_checksums and source_checksum:
                with open(target, "rb") as f:
                    target_checksum = hashlib.md5(f.read()).hexdigest()
                perform_copy = target_checksum != source_checksum
            elif source_stat.st_mtime != target_stat.st_mtime:
                perform_copy = True
            elif source_stat.st_size != target_stat.st_size:
                perform_copy = True
        else:
            # Copy if it does not exist
            perform_copy = True

        if perform_copy:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target)
