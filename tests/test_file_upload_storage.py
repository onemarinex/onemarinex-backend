import asyncio
import base64
import io
import os
import pathlib
import re
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from app.api.v1.routes_files import MAX_UPLOAD_BYTES, read_private_file, upload_file
from app.services import storage


class StoragePolicyTests(unittest.TestCase):
    def test_default_policy_is_local(self):
        self.assertEqual(storage.STORAGE_BACKEND, "local")

    def test_explicit_local_backend_ignores_present_spaces_credentials(self):
        with patch.multiple(
            storage,
            STORAGE_BACKEND="local",
            SPACES_KEY="production-key",
            SPACES_SECRET="production-secret",
            SPACES_BUCKET="production-bucket",
        ):
            self.assertFalse(storage.spaces_enabled())

    def test_explicit_spaces_requires_complete_configuration(self):
        with patch.multiple(
            storage, STORAGE_BACKEND="spaces", SPACES_KEY="", SPACES_SECRET="", SPACES_BUCKET=""
        ):
            with self.assertRaises(RuntimeError):
                storage.spaces_enabled()


class FileUploadTests(unittest.TestCase):
    def run_upload(self, file):
        return asyncio.run(
            upload_file(file=file, current_user=SimpleNamespace(id=42, role="agent"))
        )

    def test_upload_is_persisted_and_returns_reference(self):
        file = UploadFile(filename="safety report.pdf", file=io.BytesIO(b"%PDF-test"), headers=Headers({"content-type": "application/pdf"}))
        with patch("app.api.v1.routes_files.save_fileobj", return_value="/uploads/general/42/file.pdf") as save:
            result = self.run_upload(file)
        self.assertEqual(result["url"], "/uploads/general/42/file.pdf")
        self.assertEqual(result["size"], 9)
        self.assertIn("general/42/", save.call_args.args[1])

    def test_unsupported_content_type_is_rejected(self):
        file = UploadFile(filename="script.html", file=io.BytesIO(b"<script>"), headers=Headers({"content-type": "text/html"}))
        with self.assertRaises(HTTPException) as error:
            self.run_upload(file)
        self.assertEqual(error.exception.status_code, 415)

    def test_oversize_upload_is_rejected(self):
        file = UploadFile(filename="huge.pdf", file=io.BytesIO(b"x" * (MAX_UPLOAD_BYTES + 1)), headers=Headers({"content-type": "application/pdf"}))
        with self.assertRaises(HTTPException) as error:
            self.run_upload(file)
        self.assertEqual(error.exception.status_code, 413)


class PrivateStorageTests(unittest.TestCase):
    """A sensitive file must not be readable by anyone who guesses its name.

    The read-only audit's finding was that the defaults "are not an
    identity-document storage policy". They were not a policy at all: nothing
    expressed which files were sensitive, so a vendor's bank details took
    whatever the bucket's default was -- `public-read` -- and on local disk
    landed under the unauthenticated /uploads mount under a guessable
    `<timestamp>_<their filename>` name.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.public_root = os.path.join(self.root, "uploads")
        self.private_root = os.path.join(self.root, "uploads-private")
        patcher = patch.multiple(
            storage,
            UPLOAD_DIR=self.public_root,
            PRIVATE_UPLOAD_DIR=self.private_root,
            STORAGE_BACKEND="local",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.root, True)

    def store(self, public):
        return storage.save_fileobj(
            io.BytesIO(b"passport-scan"), "docs/secret.pdf",
            content_type="application/pdf", public=public,
        )

    def test_private_file_is_not_written_under_the_static_mount(self):
        stored = self.store(public=False)
        self.assertTrue(stored.startswith("private://"))
        # app.main mounts UPLOAD_DIR with StaticFiles, which asks for nothing.
        self.assertFalse(os.path.exists(os.path.join(self.public_root, "docs/secret.pdf")))
        self.assertTrue(os.path.isfile(os.path.join(self.private_root, "docs/secret.pdf")))

    def test_public_file_still_goes_to_the_static_mount(self):
        stored = self.store(public=True)
        self.assertTrue(stored.endswith("/uploads/docs/secret.pdf"))
        self.assertTrue(os.path.isfile(os.path.join(self.public_root, "docs/secret.pdf")))

    def test_sensitivity_must_be_stated(self):
        # No default: the environment cannot know whether a file is a logo or
        # a passport, and letting it guess is the whole finding.
        with self.assertRaises(TypeError):
            storage.save_fileobj(io.BytesIO(b"x"), "k")

    def test_private_spaces_object_never_gets_a_public_acl(self):
        client = MagicMock()
        with patch.multiple(
            storage, STORAGE_BACKEND="spaces", SPACES_KEY="k", SPACES_SECRET="s",
            SPACES_BUCKET="b", SPACES_PUBLIC=True,
        ), patch.object(storage, "_get_client", return_value=client):
            stored = self.store(public=False)
        self.assertEqual(stored, "spaces://docs/secret.pdf")
        self.assertNotIn("ACL", client.upload_fileobj.call_args.kwargs["ExtraArgs"])

    def test_resolve_signs_a_private_reference_and_the_url_expires(self):
        stored = self.store(public=False)
        url = storage.resolve(stored)
        self.assertTrue(url.startswith("/api/v1/files/private/"))
        token = url.rsplit("/", 1)[-1]
        self.assertEqual(storage.verify_private_token(token), "docs/secret.pdf")

    def test_an_expired_token_is_refused(self):
        token = storage.sign_private_key("docs/secret.pdf", ttl_seconds=-1)
        self.assertIsNone(storage.verify_private_token(token))

    def test_a_token_signed_with_another_key_is_refused(self):
        with patch.object(storage, "PRIVATE_URL_TTL_SECONDS", 3600):
            token = storage.sign_private_key("docs/secret.pdf")
        forged = token.split(".")[0] + "." + base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
        self.assertIsNone(storage.verify_private_token(forged))

    def test_changing_the_key_inside_a_valid_token_is_refused(self):
        token = storage.sign_private_key("docs/secret.pdf")
        payload, digest = token.split(".", 1)
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
        _, expires = decoded.rsplit("|", 1)
        swapped = base64.urlsafe_b64encode(f"docs/other.pdf|{expires}".encode()).decode().rstrip("=")
        self.assertIsNone(storage.verify_private_token(f"{swapped}.{digest}"))

    def test_a_key_that_climbs_out_of_the_private_root_reads_nothing(self):
        outside = os.path.join(self.root, "outside.txt")
        with open(outside, "w") as handle:
            handle.write("not yours")
        self.assertIsNone(storage.read_bytes("private://../outside.txt"))

    def test_references_written_before_private_storage_existed_still_resolve(self):
        # The guarantee that makes this deployable without touching a row.
        for legacy in ("/uploads/2026_passport.pdf", "https://cdn.example.com/b/logo.png"):
            self.assertEqual(storage.resolve(legacy), legacy)

    def test_a_private_file_is_deleted_from_the_private_root(self):
        stored = self.store(public=False)
        storage.delete(stored)
        self.assertFalse(os.path.exists(os.path.join(self.private_root, "docs/secret.pdf")))


class PrivateFileRouteTests(unittest.TestCase):
    def test_a_forged_token_is_a_404_and_not_a_hint(self):
        with self.assertRaises(HTTPException) as error:
            read_private_file("not-a-token")
        self.assertEqual(error.exception.status_code, 404)

    def test_a_valid_token_serves_the_bytes_uncacheable(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "docs"))
        with open(os.path.join(root, "docs/secret.pdf"), "wb") as handle:
            handle.write(b"%PDF-private")
        with patch.object(storage, "PRIVATE_UPLOAD_DIR", root):
            response = read_private_file(storage.sign_private_key("docs/secret.pdf"))
        self.assertEqual(response.body, b"%PDF-private")
        self.assertEqual(response.headers["cache-control"], "private, no-store")


class EveryUploadStatesItsSensitivityTests(unittest.TestCase):
    """Read the call sites, because a new one that omits `public=` is the way back.

    `save_fileobj` and `save_upload` both require the keyword, so omitting it
    is a TypeError at runtime -- but only on a path someone exercises. This
    fails at the source instead.
    """

    def test_no_call_site_relies_on_a_default(self):
        root = pathlib.Path(__file__).resolve().parent.parent / "app"
        calls = []
        for path in root.rglob("*.py"):
            if path.name == "storage.py":
                continue
            text = path.read_text()
            for match in re.finditer(r"(save_fileobj|save_upload)\s*\(", text):
                start = match.end()
                depth, index = 1, start
                while index < len(text) and depth:
                    depth += (text[index] == "(") - (text[index] == ")")
                    index += 1
                if "public=" not in text[start:index]:
                    line = text[:match.start()].count("\n") + 1
                    calls.append(f"{path.relative_to(root.parent)}:{line}")
        self.assertEqual(calls, [], f"these uploads do not say whether they are public: {calls}")


if __name__ == "__main__":
    unittest.main()
