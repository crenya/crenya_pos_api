"""Every doctype's controller must define the class Frappe looks for, or its desk page is a 500.

frappe/model/base_document.py import_controller() imports
`{app}.{scrub(module)}.doctype.{scrub(name)}.{scrub(name)}` and reads the class
`name.replace(" ", "").replace("-", "")` from it; a missing class raises ImportError(doctype).
`frappe.has_permission` runs that import, so one wrong class name broke the desk boot (sidebar)
for every user whose roles reach the doctype ("ERPTrue HMS Settings" kept `class TunaHMSSetting`
from an old rename, 2026-10-16). The rules below mirror Frappe's own, hyphens included.

Pure python, no site needed: `python -m unittest discover -s <app>/tests -p 'test_doctype_*.py'`.
Under `bench run-tests` (a site is connected) it also imports each controller for real.
"""

import ast
import glob
import importlib
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# The app package is the folder that holds hooks.py: this file's parent (<app>/tests/) or a child.
PKG = next(
	p
	for p in [os.path.dirname(HERE), *sorted(glob.glob(os.path.join(HERE, "*", "")))]
	if os.path.exists(os.path.join(p, "hooks.py"))
)
APP = os.path.basename(os.path.normpath(PKG))
# Fewer doctypes than this means the scan is looking in the wrong place, not that all is well.
MIN_DOCTYPES = 5


def scrub(txt):
	# frappe.scrub: each space and each hyphen becomes "_" (no collapsing), then lower-case.
	return txt.replace(" ", "_").replace("-", "_").lower()


def class_name(doctype):
	# import_controller: classname = doctype.replace(" ", "").replace("-", "")
	return doctype.replace(" ", "").replace("-", "")


def standard_doctypes():
	"""(name, module, json path) for every non-custom DocType JSON in the app."""
	out = []
	for path in sorted(glob.glob(os.path.join(PKG, "**", "doctype", "*", "*.json"), recursive=True)):
		with open(path) as f:
			j = json.load(f)
		if isinstance(j, dict) and j.get("doctype") == "DocType" and not j.get("custom"):
			out.append((j["name"], j["module"], path))
	return out


def top_level_names(source):
	"""Names a module defines at top level: classes, assignments (aliases) and imports."""
	names = set()
	for node in ast.parse(source).body:
		if isinstance(node, ast.ClassDef):
			names.add(node.name)
		elif isinstance(node, ast.Assign):
			names.update(t.id for t in node.targets if isinstance(t, ast.Name))
		elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
			names.add(node.target.id)
		elif isinstance(node, ast.ImportFrom | ast.Import):
			names.update((a.asname or a.name).split(".")[0] for a in node.names)
	return names


def site_connected():
	try:
		import frappe

		return bool(getattr(frappe.local, "site", None)) and bool(getattr(frappe.local, "db", None))
	except Exception:
		return False


class TestDoctypeControllers(unittest.TestCase):
	def test_every_controller_defines_the_class_frappe_imports(self):
		doctypes = standard_doctypes()
		self.assertGreaterEqual(len(doctypes), MIN_DOCTYPES, f"found too few doctypes under {PKG}")
		bad = []
		for name, module, _path in doctypes:
			controller = os.path.join(PKG, scrub(module), "doctype", scrub(name), scrub(name) + ".py")
			if not os.path.exists(controller):
				bad.append(f"{name}: no controller at {os.path.relpath(controller, PKG)}")
				continue
			with open(controller) as f:
				if class_name(name) not in top_level_names(f.read()):
					bad.append(f"{name}: {os.path.relpath(controller, PKG)} has no class {class_name(name)}")
		self.assertEqual(bad, [], "Frappe raises ImportError for these doctypes (desk 500)")

	@unittest.skipUnless(site_connected(), "needs a bench site (bench run-tests)")
	def test_every_controller_imports_on_a_site(self):
		from frappe.model.base_document import BaseDocument

		bad = []
		for name, module, _path in standard_doctypes():
			dotted = f"{scrub(APP)}.{scrub(module)}.doctype.{scrub(name)}.{scrub(name)}"
			try:
				cls = getattr(importlib.import_module(dotted), class_name(name), None)
			except Exception as e:
				bad.append(f"{name}: import {dotted} failed: {e!r}")
				continue
			if cls is None or not isinstance(cls, type) or not issubclass(cls, BaseDocument):
				bad.append(f"{name}: {dotted}.{class_name(name)} missing or not a BaseDocument")
		self.assertEqual(bad, [], "Frappe raises ImportError for these doctypes (desk 500)")


if __name__ == "__main__":
	unittest.main()
