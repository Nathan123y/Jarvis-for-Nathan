import ast
import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from actions import file_processor as fp


def _study_methods(render):
    """JarvisLive._study_file and _flush_pending_document, without starting Jarvis."""
    source = Path(__file__).resolve().parents[1] / "main.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    owner = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "JarvisLive")
    wanted = {"_study_file", "_flush_pending_document"}
    methods = [n for n in owner.body if isinstance(n, ast.AsyncFunctionDef) and n.name in wanted]
    stub = ast.ClassDef(name="Study", bases=[], keywords=[], body=methods, decorator_list=[])
    code = ast.fix_missing_locations(ast.Module(body=[stub], type_ignores=[]))
    namespace = {"render_for_study": render, "_STUDY_GUIDE": "GUIDE",
                 "locate_file": lambda p: Path(p)}
    exec(compile(code, str(source), "exec"), namespace)
    return namespace["Study"]


class StudyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _pdf(self, pages: int) -> Path:
        from PIL import Image
        path = self.root / "homework.pdf"
        frames = [Image.new("RGB", (612, 792), "white") for _ in range(pages)]
        frames[0].save(path, "PDF", save_all=True, append_images=frames[1:])
        return path

    def test_parse_pages(self):
        self.assertEqual(fp._parse_pages("", 3), [0, 1, 2])
        self.assertEqual(fp._parse_pages("", 20), list(range(fp._STUDY_MAX_PAGES)))
        self.assertEqual(fp._parse_pages("2", 3), [1])
        self.assertEqual(fp._parse_pages("1-3, 9", 10), [0, 1, 2, 8])
        self.assertEqual(fp._parse_pages("3-1", 3), [0, 1, 2])
        self.assertEqual(fp._parse_pages("7", 3), [])

    def test_renders_every_pdf_page_as_jpeg(self):
        images, note = fp.render_for_study(str(self._pdf(3)))
        self.assertEqual(len(images), 3)
        self.assertTrue(all(img[:2] == b"\xff\xd8" for img in images))
        self.assertIn("page(s) 1, 2, 3 of 3", note)

    def test_renders_chosen_pages_and_mentions_the_rest(self):
        images, note = fp.render_for_study(str(self._pdf(3)), "2")
        self.assertEqual(len(images), 1)
        self.assertIn("2 of 3", note)
        self.assertIn("pages=", note)

    def test_image_is_passed_through(self):
        from PIL import Image
        path = self.root / "diagram.png"
        Image.new("RGB", (3000, 1000), "white").save(path)
        images, note = fp.render_for_study(str(path))
        self.assertEqual(len(images), 1)
        with Image.open(__import__("io").BytesIO(images[0])) as img:
            self.assertEqual(img.width, fp._STUDY_WIDTH)

    def test_unsupported_and_missing_files_explain_why(self):
        doc = self.root / "notes.txt"
        doc.write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "PDFs and images"):
            fp.render_for_study(str(doc))
        with self.assertRaisesRegex(ValueError, "not found"):
            fp.render_for_study(str(self.root / "gone.pdf"))
        with self.assertRaisesRegex(ValueError, "none matched"):
            fp.render_for_study(str(self._pdf(2)), "5")

    def test_image_only_pdf_text_extraction_points_to_study(self):
        result = fp.file_processor({"action": "extract_text", "file_path": str(self._pdf(1))})
        self.assertIn("study", result)

    def test_tool_declares_study_and_pages(self):
        props = fp.TOOL["parameters"]["properties"]
        self.assertIn("pages", props)
        self.assertIn("study", props["action"]["description"])


class StudyWiringTests(unittest.IsolatedAsyncioTestCase):
    def jarvis(self, render, current_file=None):
        sent = []

        class Session:
            async def send_client_content(self, **kwargs):
                sent.append(kwargs)

        jarvis = _study_methods(render)()
        jarvis.ui = SimpleNamespace(current_file=current_file)
        jarvis.session = Session()
        jarvis._pending_document = None
        return jarvis, sent

    async def test_pages_follow_the_tool_result_with_the_tutor_guide(self):
        seen = {}
        def render(path, pages):
            seen.update(path=path, pages=pages)
            return [b"page-1", b"page-2"], "hw.pdf: page(s) 1, 2 of 2"
        jarvis, sent = self.jarvis(render, current_file="/uploads/hw.pdf")
        result = await jarvis._study_file({"instruction": "problem 3"}, asyncio.get_running_loop())
        self.assertIn("[DOCUMENT_ATTACHED]", result)
        self.assertEqual(seen, {"path": "/uploads/hw.pdf", "pages": ""})
        await jarvis._flush_pending_document()
        parts = sent[0]["turns"]["parts"]
        self.assertEqual([base64.b64decode(p["inline_data"]["data"]) for p in parts[:2]],
                         [b"page-1", b"page-2"])
        self.assertIn("GUIDE", parts[2]["text"])
        self.assertIn("problem 3", parts[2]["text"])
        self.assertTrue(sent[0]["turn_complete"])
        await jarvis._flush_pending_document()
        self.assertEqual(len(sent), 1, "the pages are sent once")

    async def test_the_studied_file_is_remembered_for_the_next_problem(self):
        seen = []
        def render(path, pages):
            seen.append((path, pages))
            return [b"p"], "hw.pdf"
        jarvis, sent = self.jarvis(render)
        await jarvis._study_file({"file_path": "/x/hw.pdf"}, asyncio.get_running_loop())
        result = await jarvis._study_file({"pages": "2"}, asyncio.get_running_loop())
        self.assertIn("[DOCUMENT_ATTACHED]", result)
        self.assertEqual(seen, [("/x/hw.pdf", ""), ("/x/hw.pdf", "2")])

    async def test_a_path_that_cannot_be_found_says_how_to_find_it(self):
        jarvis, sent = self.jarvis(lambda p, pg: self.fail("nothing to render"))
        Study = type(jarvis)
        Study._study_file.__globals__["locate_file"] = lambda p: None
        try:
            result = await jarvis._study_file({"file_path": "/Users/nathan/x.pdf"},
                                              asyncio.get_running_loop())
        finally:
            Study._study_file.__globals__["locate_file"] = lambda p: Path(p)
        self.assertIn("File not found", result)
        self.assertIn("file_controller", result)

    async def test_no_file_or_a_render_error_is_said_not_sent(self):
        jarvis, sent = self.jarvis(lambda p, pg: self.fail("nothing to render"))
        result = await jarvis._study_file({}, asyncio.get_running_loop())
        self.assertIn("No file is loaded", result)

        def broken(path, pages):
            raise ValueError("Study mode shows PDFs and images; notes.txt is text.")
        jarvis, sent = self.jarvis(broken, current_file="/x/notes.txt")
        result = await jarvis._study_file({}, asyncio.get_running_loop())
        self.assertIn("PDFs and images", result)
        await jarvis._flush_pending_document()
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
