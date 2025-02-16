import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock
from zipfile import ZipFile

from django.core.management import call_command
from django.test import override_settings
from django.test import TestCase
from django.utils import timezone
from documents.management.commands import document_exporter
from documents.models import Comment
from documents.models import Correspondent
from documents.models import Document
from documents.models import DocumentType
from documents.models import StoragePath
from documents.models import Tag
from documents.models import User
from documents.sanity_checker import check_sanity
from documents.settings import EXPORTER_FILE_NAME
from documents.tests.utils import DirectoriesMixin
from documents.tests.utils import paperless_environment


class TestExportImport(DirectoriesMixin, TestCase):
    def setUp(self) -> None:
        self.target = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.target)

        self.user = User.objects.create(username="temp_admin")

        self.d1 = Document.objects.create(
            content="Content",
            checksum="42995833e01aea9b3edee44bbfdd7ce1",
            archive_checksum="62acb0bcbfbcaa62ca6ad3668e4e404b",
            title="wow1",
            filename="0000001.pdf",
            mime_type="application/pdf",
            archive_filename="0000001.pdf",
        )
        self.d2 = Document.objects.create(
            content="Content",
            checksum="9c9691e51741c1f4f41a20896af31770",
            title="wow2",
            filename="0000002.pdf",
            mime_type="application/pdf",
        )
        self.d3 = Document.objects.create(
            content="Content",
            checksum="d38d7ed02e988e072caf924e0f3fcb76",
            title="wow2",
            filename="0000003.pdf",
            mime_type="application/pdf",
        )
        self.d4 = Document.objects.create(
            content="Content",
            checksum="82186aaa94f0b98697d704b90fd1c072",
            title="wow_dec",
            filename="0000004.pdf.gpg",
            mime_type="application/pdf",
            storage_type=Document.STORAGE_TYPE_GPG,
        )

        self.comment = Comment.objects.create(
            comment="This is a comment. amaze.",
            document=self.d1,
            user=self.user,
        )

        self.t1 = Tag.objects.create(name="t")
        self.dt1 = DocumentType.objects.create(name="dt")
        self.c1 = Correspondent.objects.create(name="c")
        self.sp1 = StoragePath.objects.create(path="{created_year}-{title}")

        self.d1.tags.add(self.t1)
        self.d1.correspondent = self.c1
        self.d1.document_type = self.dt1
        self.d1.save()
        self.d4.storage_path = self.sp1
        self.d4.save()
        super().setUp()

    def _get_document_from_manifest(self, manifest, id):
        f = list(
            filter(
                lambda d: d["model"] == "documents.document" and d["pk"] == id,
                manifest,
            ),
        )
        if len(f) == 1:
            return f[0]
        else:
            raise ValueError(f"document with id {id} does not exist in manifest")

    @override_settings(PASSPHRASE="test")
    def _do_export(
        self,
        use_filename_format=False,
        compare_checksums=False,
        delete=False,
        no_archive=False,
        no_thumbnail=False,
        split_manifest=False,
    ):
        args = ["document_exporter", self.target]
        if use_filename_format:
            args += ["--use-filename-format"]
        if compare_checksums:
            args += ["--compare-checksums"]
        if delete:
            args += ["--delete"]
        if no_archive:
            args += ["--no-archive"]
        if no_thumbnail:
            args += ["--no-thumbnail"]
        if split_manifest:
            args += ["--split-manifest"]

        call_command(*args)

        with open(os.path.join(self.target, "manifest.json")) as f:
            manifest = json.load(f)

        return manifest

    def test_exporter(self, use_filename_format=False):
        shutil.rmtree(os.path.join(self.dirs.media_dir, "documents"))
        shutil.copytree(
            os.path.join(os.path.dirname(__file__), "samples", "documents"),
            os.path.join(self.dirs.media_dir, "documents"),
        )

        manifest = self._do_export(use_filename_format=use_filename_format)

        self.assertEqual(len(manifest), 11)
        self.assertEqual(
            len(list(filter(lambda e: e["model"] == "documents.document", manifest))),
            4,
        )

        self.assertTrue(os.path.exists(os.path.join(self.target, "manifest.json")))

        self.assertEqual(
            self._get_document_from_manifest(manifest, self.d1.id)["fields"]["title"],
            "wow1",
        )
        self.assertEqual(
            self._get_document_from_manifest(manifest, self.d2.id)["fields"]["title"],
            "wow2",
        )
        self.assertEqual(
            self._get_document_from_manifest(manifest, self.d3.id)["fields"]["title"],
            "wow2",
        )
        self.assertEqual(
            self._get_document_from_manifest(manifest, self.d4.id)["fields"]["title"],
            "wow_dec",
        )

        for element in manifest:
            if element["model"] == "documents.document":
                fname = os.path.join(
                    self.target,
                    element[document_exporter.EXPORTER_FILE_NAME],
                )
                self.assertTrue(os.path.exists(fname))
                self.assertTrue(
                    os.path.exists(
                        os.path.join(
                            self.target,
                            element[document_exporter.EXPORTER_THUMBNAIL_NAME],
                        ),
                    ),
                )

                with open(fname, "rb") as f:
                    checksum = hashlib.md5(f.read()).hexdigest()
                self.assertEqual(checksum, element["fields"]["checksum"])

                self.assertEqual(
                    element["fields"]["storage_type"],
                    Document.STORAGE_TYPE_UNENCRYPTED,
                )

                if document_exporter.EXPORTER_ARCHIVE_NAME in element:
                    fname = os.path.join(
                        self.target,
                        element[document_exporter.EXPORTER_ARCHIVE_NAME],
                    )
                    self.assertTrue(os.path.exists(fname))

                    with open(fname, "rb") as f:
                        checksum = hashlib.md5(f.read()).hexdigest()
                    self.assertEqual(checksum, element["fields"]["archive_checksum"])

            elif element["model"] == "documents.comment":
                self.assertEqual(element["fields"]["comment"], self.comment.comment)
                self.assertEqual(element["fields"]["document"], self.d1.id)
                self.assertEqual(element["fields"]["user"], self.user.id)

        with paperless_environment() as dirs:
            self.assertEqual(Document.objects.count(), 4)
            Document.objects.all().delete()
            Correspondent.objects.all().delete()
            DocumentType.objects.all().delete()
            Tag.objects.all().delete()
            self.assertEqual(Document.objects.count(), 0)

            call_command("document_importer", self.target)
            self.assertEqual(Document.objects.count(), 4)
            self.assertEqual(Tag.objects.count(), 1)
            self.assertEqual(Correspondent.objects.count(), 1)
            self.assertEqual(DocumentType.objects.count(), 1)
            self.assertEqual(StoragePath.objects.count(), 1)
            self.assertEqual(Document.objects.get(id=self.d1.id).title, "wow1")
            self.assertEqual(Document.objects.get(id=self.d2.id).title, "wow2")
            self.assertEqual(Document.objects.get(id=self.d3.id).title, "wow2")
            self.assertEqual(Document.objects.get(id=self.d4.id).title, "wow_dec")
            messages = check_sanity()
            # everything is alright after the test
            self.assertEqual(len(messages), 0)

    def test_exporter_with_filename_format(self):
        shutil.rmtree(os.path.join(self.dirs.media_dir, "documents"))
        shutil.copytree(
            os.path.join(os.path.dirname(__file__), "samples", "documents"),
            os.path.join(self.dirs.media_dir, "documents"),
        )

        with override_settings(
            FILENAME_FORMAT="{created_year}/{correspondent}/{title}",
        ):
            self.test_exporter(use_filename_format=True)

    def test_update_export_changed_time(self):
        shutil.rmtree(os.path.join(self.dirs.media_dir, "documents"))
        shutil.copytree(
            os.path.join(os.path.dirname(__file__), "samples", "documents"),
            os.path.join(self.dirs.media_dir, "documents"),
        )

        self._do_export()
        self.assertTrue(os.path.exists(os.path.join(self.target, "manifest.json")))

        st_mtime_1 = os.stat(os.path.join(self.target, "manifest.json")).st_mtime

        with mock.patch(
            "documents.management.commands.document_exporter.shutil.copy2",
        ) as m:
            self._do_export()
            m.assert_not_called()

        self.assertTrue(os.path.exists(os.path.join(self.target, "manifest.json")))
        st_mtime_2 = os.stat(os.path.join(self.target, "manifest.json")).st_mtime

        Path(self.d1.source_path).touch()

        with mock.patch(
            "documents.management.commands.document_exporter.shutil.copy2",
        ) as m:
            self._do_export()
            self.assertEqual(m.call_count, 1)

        st_mtime_3 = os.stat(os.path.join(self.target, "manifest.json")).st_mtime
        self.assertTrue(os.path.exists(os.path.join(self.target, "manifest.json")))

        self.assertNotEqual(st_mtime_1, st_mtime_2)
        self.assertNotEqual(st_mtime_2, st_mtime_3)

    def test_update_export_changed_checksum(self):
        shutil.rmtree(os.path.join(self.dirs.media_dir, "documents"))
        shutil.copytree(
            os.path.join(os.path.dirname(__file__), "samples", "documents"),
            os.path.join(self.dirs.media_dir, "documents"),
        )

        self._do_export()

        self.assertTrue(os.path.exists(os.path.join(self.target, "manifest.json")))

        with mock.patch(
            "documents.management.commands.document_exporter.shutil.copy2",
        ) as m:
            self._do_export()
            m.assert_not_called()

        self.assertTrue(os.path.exists(os.path.join(self.target, "manifest.json")))

        self.d2.checksum = "asdfasdgf3"
        self.d2.save()

        with mock.patch(
            "documents.management.commands.document_exporter.shutil.copy2",
        ) as m:
            self._do_export(compare_checksums=True)
            self.assertEqual(m.call_count, 1)

        self.assertTrue(os.path.exists(os.path.join(self.target, "manifest.json")))

    def test_update_export_deleted_document(self):
        shutil.rmtree(os.path.join(self.dirs.media_dir, "documents"))
        shutil.copytree(
            os.path.join(os.path.dirname(__file__), "samples", "documents"),
            os.path.join(self.dirs.media_dir, "documents"),
        )

        manifest = self._do_export()

        self.assertTrue(len(manifest), 7)
        doc_from_manifest = self._get_document_from_manifest(manifest, self.d3.id)
        self.assertTrue(
            os.path.isfile(
                os.path.join(self.target, doc_from_manifest[EXPORTER_FILE_NAME]),
            ),
        )
        self.d3.delete()

        manifest = self._do_export()
        self.assertRaises(
            ValueError,
            self._get_document_from_manifest,
            manifest,
            self.d3.id,
        )
        self.assertTrue(
            os.path.isfile(
                os.path.join(self.target, doc_from_manifest[EXPORTER_FILE_NAME]),
            ),
        )

        manifest = self._do_export(delete=True)
        self.assertFalse(
            os.path.isfile(
                os.path.join(self.target, doc_from_manifest[EXPORTER_FILE_NAME]),
            ),
        )

        self.assertTrue(len(manifest), 6)

    @override_settings(FILENAME_FORMAT="{title}/{correspondent}")
    def test_update_export_changed_location(self):
        shutil.rmtree(os.path.join(self.dirs.media_dir, "documents"))
        shutil.copytree(
            os.path.join(os.path.dirname(__file__), "samples", "documents"),
            os.path.join(self.dirs.media_dir, "documents"),
        )

        m = self._do_export(use_filename_format=True)
        self.assertTrue(os.path.isfile(os.path.join(self.target, "wow1", "c.pdf")))

        self.assertTrue(os.path.exists(os.path.join(self.target, "manifest.json")))

        self.d1.title = "new_title"
        self.d1.save()
        self._do_export(use_filename_format=True, delete=True)
        self.assertFalse(os.path.isfile(os.path.join(self.target, "wow1", "c.pdf")))
        self.assertFalse(os.path.isdir(os.path.join(self.target, "wow1")))
        self.assertTrue(os.path.isfile(os.path.join(self.target, "new_title", "c.pdf")))
        self.assertTrue(os.path.exists(os.path.join(self.target, "manifest.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.target, "wow2", "none.pdf")))
        self.assertTrue(
            os.path.isfile(os.path.join(self.target, "wow2", "none_01.pdf")),
        )

    def test_export_missing_files(self):

        target = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, target)
        Document.objects.create(
            checksum="AAAAAAAAAAAAAAAAA",
            title="wow",
            filename="0000004.pdf",
            mime_type="application/pdf",
        )
        self.assertRaises(FileNotFoundError, call_command, "document_exporter", target)

    @override_settings(PASSPHRASE="test")
    def test_export_zipped(self):
        """
        GIVEN:
            - Request to export documents to zipfile
        WHEN:
            - Documents are exported
        THEN:
            - Zipfile is created
            - Zipfile contains exported files
        """
        shutil.rmtree(os.path.join(self.dirs.media_dir, "documents"))
        shutil.copytree(
            os.path.join(os.path.dirname(__file__), "samples", "documents"),
            os.path.join(self.dirs.media_dir, "documents"),
        )

        args = ["document_exporter", self.target, "--zip"]

        call_command(*args)

        expected_file = os.path.join(
            self.target,
            f"export-{timezone.localdate().isoformat()}.zip",
        )

        self.assertTrue(os.path.isfile(expected_file))

        with ZipFile(expected_file) as zip:
            self.assertEqual(len(zip.namelist()), 11)
            self.assertIn("manifest.json", zip.namelist())
            self.assertIn("version.json", zip.namelist())

    @override_settings(PASSPHRASE="test")
    def test_export_zipped_format(self):
        """
        GIVEN:
            - Request to export documents to zipfile
            - Export is following filename formatting
        WHEN:
            - Documents are exported
        THEN:
            - Zipfile is created
            - Zipfile contains exported files
        """
        shutil.rmtree(os.path.join(self.dirs.media_dir, "documents"))
        shutil.copytree(
            os.path.join(os.path.dirname(__file__), "samples", "documents"),
            os.path.join(self.dirs.media_dir, "documents"),
        )

        args = ["document_exporter", self.target, "--zip", "--use-filename-format"]

        with override_settings(
            FILENAME_FORMAT="{created_year}/{correspondent}/{title}",
        ):
            call_command(*args)

        expected_file = os.path.join(
            self.target,
            f"export-{timezone.localdate().isoformat()}.zip",
        )

        self.assertTrue(os.path.isfile(expected_file))

        with ZipFile(expected_file) as zip:
            # Extras are from the directories, which also appear in the listing
            self.assertEqual(len(zip.namelist()), 14)
            self.assertIn("manifest.json", zip.namelist())
            self.assertIn("version.json", zip.namelist())

    def test_no_archive(self):
        shutil.rmtree(os.path.join(self.dirs.media_dir, "documents"))
        shutil.copytree(
            os.path.join(os.path.dirname(__file__), "samples", "documents"),
            os.path.join(self.dirs.media_dir, "documents"),
        )

        manifest = self._do_export()
        has_archive = False
        for element in manifest:
            if element["model"] == "documents.document":
                has_archive = (
                    has_archive or document_exporter.EXPORTER_ARCHIVE_NAME in element
                )
        self.assertTrue(has_archive)

        has_archive = False
        manifest = self._do_export(no_archive=True)
        for element in manifest:
            if element["model"] == "documents.document":
                has_archive = (
                    has_archive or document_exporter.EXPORTER_ARCHIVE_NAME in element
                )
        self.assertFalse(has_archive)

        with paperless_environment() as dirs:
            call_command("document_importer", self.target)
            self.assertEqual(Document.objects.count(), 4)

    def test_no_thumbnail(self):
        shutil.rmtree(os.path.join(self.dirs.media_dir, "documents"))
        shutil.copytree(
            os.path.join(os.path.dirname(__file__), "samples", "documents"),
            os.path.join(self.dirs.media_dir, "documents"),
        )

        manifest = self._do_export()
        has_thumbnail = False
        for element in manifest:
            if element["model"] == "documents.document":
                has_thumbnail = (
                    has_thumbnail
                    or document_exporter.EXPORTER_THUMBNAIL_NAME in element
                )
        self.assertTrue(has_thumbnail)

        has_thumbnail = False
        manifest = self._do_export(no_thumbnail=True)
        for element in manifest:
            if element["model"] == "documents.document":
                has_thumbnail = (
                    has_thumbnail
                    or document_exporter.EXPORTER_THUMBNAIL_NAME in element
                )
        self.assertFalse(has_thumbnail)

        with paperless_environment() as dirs:
            call_command("document_importer", self.target)
            self.assertEqual(Document.objects.count(), 4)

    def test_split_manifest(self):
        shutil.rmtree(os.path.join(self.dirs.media_dir, "documents"))
        shutil.copytree(
            os.path.join(os.path.dirname(__file__), "samples", "documents"),
            os.path.join(self.dirs.media_dir, "documents"),
        )

        manifest = self._do_export(split_manifest=True)
        has_document = False
        for element in manifest:
            has_document = has_document or element["model"] == "documents.document"
        self.assertFalse(has_document)

        with paperless_environment() as dirs:
            call_command("document_importer", self.target)
            self.assertEqual(Document.objects.count(), 4)
