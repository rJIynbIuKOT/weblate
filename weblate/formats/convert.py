#
# Copyright © 2012 - 2020 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Translate Toolkit convertor based file format wrappers."""

import codecs
import os
import shutil
from io import BytesIO
from zipfile import ZipFile

from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from translate.convert.html2po import html2po
from translate.convert.po2html import po2html
from translate.convert.po2idml import translate_idml, write_idml
from translate.convert.xliff2odf import translate_odf, write_odf
from translate.storage.idml import INLINE_ELEMENTS, NO_TRANSLATE_ELEMENTS, open_idml
from translate.storage.odf_io import open_odf
from translate.storage.odf_shared import inline_elements, no_translate_content_elements
from translate.storage.po import pofile
from translate.storage.test_factory import BaseTestFactory
from translate.storage.xliff import xlifffile
from translate.storage.xml_extract.extract import (
    IdMaker,
    ParseState,
    build_idml_store,
    build_store,
    make_postore_adder,
)

from weblate.formats.base import TranslationFormat
from weblate.formats.ttkit import TTKitUnit, XliffUnit
from weblate.utils.errors import report_error


class ConvertUnit(TTKitUnit):
    def is_translated(self):
        """Check whether unit is translated."""
        return self.unit is not None

    def is_fuzzy(self, fallback=False):
        """Check whether unit needs editing."""
        return fallback

    @cached_property
    def locations(self):
        return ""

    @cached_property
    def context(self):
        """Return context of message."""
        return "".join(self.mainunit.getlocations())


class FileNameUnit(ConvertUnit):
    @cached_property
    def context(self):
        """Return context of message."""
        return "".join(
            location.rsplit("/", 1)[1] for location in self.mainunit.getlocations()
        )


class ConvertFormat(TranslationFormat):
    """
    Base class for convert based formats.

    This always uses intermediate representation.
    """

    monolingual = True
    can_add_unit = False
    unit_class = ConvertUnit
    autoaddon = {"weblate.flags.same_edit": {}}

    def save_content(self, handle):
        """Store content to file."""
        raise NotImplementedError()

    def save(self):
        """Save underlaying store to disk."""
        self.save_atomic(self.storefile, self.save_content)

    @staticmethod
    def convertfile(storefile):
        raise NotImplementedError()

    @classmethod
    def load(cls, storefile):
        # Did we get file or filename?
        if not hasattr(storefile, "read"):
            storefile = open(storefile, "rb")
        # Adjust store to have translations
        store = cls.convertfile(storefile)
        for unit in store.units:
            if unit.isheader():
                continue
            unit.target = unit.source
            unit.rich_target = unit.rich_source
        return store

    @classmethod
    def create_new_file(cls, filename, language, base):
        """Handle creation of new translation file."""
        if not base:
            raise ValueError("Not supported")
        # Copy file
        shutil.copy(base, filename)

    @classmethod
    def is_valid_base_for_new(cls, base, monolingual):
        """Check whether base is valid."""
        if not base:
            return False
        try:
            cls.load(base)
            return True
        except Exception:
            report_error(cause="File parse error")
            return False

    @classmethod
    def get_class(cls):
        # This needs translate-toolkit 3.0.0, check can be
        # removed once dependency is raised
        if not hasattr(BaseTestFactory, "test_getobject_store"):
            raise ImportError("Needs newer translate-toolkit")

    def add_unit(self, ttkit_unit):
        raise ValueError("Not supported")

    def create_unit(self, key, source):
        raise ValueError("Not supported")


class HTMLFormat(ConvertFormat):
    name = _("HTML file")
    autoload = ("*.htm", "*.html")
    format_id = "html"
    check_flags = ("safe-html", "strict-same")
    unit_class = FileNameUnit

    @staticmethod
    def convertfile(storefile):
        return html2po().convertfile(storefile, os.path.basename(storefile.name))

    def save_content(self, handle):
        """Store content to file."""
        convertor = po2html()
        templatename = self.template_store.storefile
        if hasattr(templatename, "name"):
            templatename = templatename.name
        with open(templatename, "rb") as templatefile:
            outputstring = convertor.mergestore(
                self.store, templatefile, includefuzzy=False
            )
        handle.write(outputstring.encode("utf-8"))

    @staticmethod
    def mimetype():
        """Return most common mime type for format."""
        return "text/html"

    @staticmethod
    def extension():
        """Return most common file extension for format."""
        return "html"


class OpenDocumentFormat(ConvertFormat):
    name = _("OpenDocument file")
    autoload = (
        "*.sxw",
        "*.odt",
        "*.ods",
        "*.odp",
        "*.odg",
        "*.odc",
        "*.odf",
        "*.odi",
        "*.odm",
        "*.ott",
        "*.ots",
        "*.otp",
        "*.otg",
        "*.otc",
        "*.otf",
        "*.oti",
        "*.oth",
    )
    format_id = "odf"
    check_flags = ("strict-same",)
    unit_class = XliffUnit

    @staticmethod
    def convertfile(storefile):
        store = xlifffile()
        store.setfilename(store.getfilenode("NoName"), os.path.basename(storefile.name))
        contents = open_odf(storefile)
        for data in contents.values():
            parse_state = ParseState(no_translate_content_elements, inline_elements)
            build_store(BytesIO(data), store, parse_state)
        return store

    def save_content(self, handle):
        """Store content to file."""
        templatename = self.template_store.storefile
        if hasattr(templatename, "name"):
            templatename = templatename.name
        with open(templatename, "rb") as templatefile:
            dom_trees = translate_odf(templatefile, self.store)
            write_odf(templatefile, handle, dom_trees)

    @staticmethod
    def mimetype():
        """Return most common mime type for format."""
        return "application/vnd.oasis.opendocument.text"

    @staticmethod
    def extension():
        """Return most common file extension for format."""
        return "odt"


class IDMLFormat(ConvertFormat):
    name = _("IDML file")
    autoload = ("*.idml", "*.idms")
    format_id = "idml"
    check_flags = ("strict-same",)

    @staticmethod
    def convertfile(storefile):
        store = pofile()

        contents = open_idml(storefile)

        # Create it here to avoid having repeated ids.
        id_maker = IdMaker()

        for filename, translatable_file in contents.items():
            parse_state = ParseState(NO_TRANSLATE_ELEMENTS, INLINE_ELEMENTS)
            po_store_adder = make_postore_adder(store, id_maker, filename)
            build_idml_store(
                BytesIO(translatable_file),
                store,
                parse_state,
                store_adder=po_store_adder,
            )

        return store

    def save_content(self, handle):
        """Store content to file."""
        templatename = self.template_store.storefile
        if hasattr(templatename, "name"):
            templatename = templatename.name
        with ZipFile(templatename, "r") as template_zip:
            translatable_files = [
                filename
                for filename in template_zip.namelist()
                if filename.startswith("Stories/")
            ]

            dom_trees = translate_idml(templatename, self.store, translatable_files)

            write_idml(template_zip, handle, dom_trees)

    @staticmethod
    def mimetype():
        """Return most common mime type for format."""
        return "application/octet-stream"

    @staticmethod
    def extension():
        """Return most common file extension for format."""
        return "idml"


class WindowsRCFormat(ConvertFormat):
    name = _("RC file")
    format_id = "rc"
    autoload = ("*.rc",)
    language_format = "bcp"

    @staticmethod
    def mimetype():
        """Return most common media type for format."""
        return "text/plain"

    @staticmethod
    def extension():
        """Return most common file extension for format."""
        return "rc"

    @classmethod
    def get_class(cls):
        # This needs translate-toolkit 3.0.0 and pyparsing optional dep
        from translate.storage.rc import rc_statement

        return rc_statement

    @staticmethod
    def convertfile(storefile):
        from translate.storage.rc import rcfile
        from translate.convert.rc2po import rc2po

        input_store = rcfile(storefile)
        convertor = rc2po()
        store = convertor.convert_store(input_store)
        store.rcfile = input_store
        return store

    def save_content(self, handle):
        """Store content to file."""
        from translate.convert.po2rc import rerc

        # Fallback language
        lang = "LANG_ENGLISH"
        sublang = "SUBLANG_DEFAULT"

        # Keep existing language tags
        rcfile = self.store.rcfile
        if rcfile.lang:
            lang = rcfile.lang
            if rcfile.sublang:
                sublang = rcfile.sublang

        templatename = self.template_store.storefile
        if hasattr(templatename, "name"):
            templatename = templatename.name
        with open(templatename, "rb") as templatefile:
            convertor = rerc(templatefile, lang=lang, sublang=sublang)
            outputrclines = convertor.convertstore(self.store)
            try:
                handle.write(outputrclines.encode("cp1252"))
            except UnicodeEncodeError:
                handle.write(codecs.BOM_UTF16_LE)
                handle.write(outputrclines.encode("utf-16-le"))
