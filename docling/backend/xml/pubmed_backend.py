import logging
from io import BytesIO
import os
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple, Union
from bs4 import BeautifulSoup
from docling_core.types.doc import (
    DocItemLabel,
    DoclingDocument,
    DocumentOrigin,
    GroupLabel,
    TableCell,
    TableData,
    ImageRef,
)
from lxml import etree
from typing_extensions import TypedDict, override
from docling.backend.abstract_backend import DeclarativeDocumentBackend
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import InputDocument
from docling_core.types.doc.document import NodeItem
from PIL import Image
from docling_core.types.doc import Size

_log = logging.getLogger(__name__)


class Paragraph(TypedDict):
    text: str
    headers: list[str]


class Author(TypedDict):
    name: str
    affiliation_names: list[str]


class Table(TypedDict):
    label: str
    caption: str
    content: str


class FigureCaption(TypedDict):
    label: str
    caption: str


class Reference(TypedDict):
    author_names: str
    title: str
    journal: str
    year: str


class XMLComponents(TypedDict):
    title: str
    authors: list[Author]
    abstract: str
    paragraphs: list[Paragraph]
    tables: list[Table]
    figure_captions: list[FigureCaption]
    references: list[Reference]


class PubMedDocumentBackend(DeclarativeDocumentBackend):
    """
    The code from this document backend has been developed by modifying parts of the PubMed Parser library (version 0.5.0, released on 12.08.2024):
    Achakulvisut et al., (2020).
    Pubmed Parser: A Python Parser for PubMed Open-Access XML Subset and MEDLINE XML Dataset XML Dataset.
    Journal of Open Source Software, 5(46), 1979,
    https://doi.org/10.21105/joss.01979
    """

    @override
    def __init__(self, in_doc: "InputDocument", path_or_stream: Union[BytesIO, Path]):
        super().__init__(in_doc, path_or_stream)
        self.path_or_stream = path_or_stream

        self.path = in_doc.file

        # Initialize parents for the document hierarchy
        self.parents: dict = {}

        self.valid = False
        try:
            if isinstance(self.path_or_stream, BytesIO):
                self.path_or_stream.seek(0)
            self.tree: etree._ElementTree = etree.parse(self.path_or_stream)
            if "/NLM//DTD JATS" in self.tree.docinfo.public_id:
                self.valid = True
        except Exception as exc:
            raise RuntimeError(
                f"Could not initialize PubMed backend for file with hash {self.document_hash}."
            ) from exc

    @override
    def is_valid(self) -> bool:
        return self.valid

    @classmethod
    @override
    def supports_pagination(cls) -> bool:
        return False

    @override
    def unload(self):
        if isinstance(self.path_or_stream, BytesIO):
            self.path_or_stream.close()
        self.path_or_stream = None

    @classmethod
    @override
    def supported_formats(cls) -> Set[InputFormat]:
        return {InputFormat.XML_PUBMED}

    @override
    def convert(self) -> DoclingDocument:
        # Create empty document
        origin = DocumentOrigin(
            filename=self.file.name or "file",
            mimetype="application/xml",
            binary_hash=self.document_hash,
        )
        doc = DoclingDocument(name=self.file.stem or "file", origin=origin)

        _log.debug("Trying to convert PubMed XML document...")

        # Get parsed XML components
        # xml_components: XMLComponents = self._parse()

        # # Add XML components to the document
        # doc = self._populate_document(doc, xml_components)

        doc = self._parse_and_populate(doc)

        return doc

    def _parse_title(self) -> str:
        title: str = " ".join(
            [
                t.replace("\n", "")
                for t in self.tree.xpath(".//title-group/article-title")[0].itertext()
            ]
        )
        return title

    def _parse_authors(self) -> list[Author]:
        # Get mapping between affiliation ids and names
        affiliation_names = []
        for affiliation_node in self.tree.xpath(".//aff[@id]"):
            affiliation_names.append(
                ": ".join([t for t in affiliation_node.itertext() if t != "\n"])
            )
        affiliation_ids_names = {
            id: name
            for id, name in zip(self.tree.xpath(".//aff[@id]/@id"), affiliation_names)
        }

        # Get author names and affiliation names
        authors: list[Author] = []
        for author_node in self.tree.xpath(
            './/contrib-group/contrib[@contrib-type="author"]'
        ):
            author: Author = {
                "name": "",
                "affiliation_names": [],
            }

            # Affiliation names
            affiliation_ids = [
                a.attrib["rid"] for a in author_node.xpath('xref[@ref-type="aff"]')
            ]
            for id in affiliation_ids:
                if id in affiliation_ids_names:
                    author["affiliation_names"].append(affiliation_ids_names[id])

            surnames = author_node.xpath("name/surname")
            given_names = author_node.xpath("name/given-names")

            if len(surnames) > 0 and len(given_names) > 0:
                given_name = given_names[0].text
                author["name"] = (
                    surnames[0].text + " " + given_name if given_name else ""
                )

            authors.append(author)
        return authors

    def _parse_abstract(self) -> str:
        texts = []
        for abstract_node in self.tree.xpath(".//abstract"):
            for text in abstract_node.itertext():
                texts.append(text.replace("\n", ""))
        abstract: str = "".join(texts)
        return abstract

    def _parse_references(self) -> list[Reference]:
        references: list[Reference] = []
        for reference_node_abs in self.tree.xpath(".//ref-list/ref"):
            reference: Reference = {
                "author_names": "",
                "title": "",
                "journal": "",
                "year": "",
            }
            reference_node: Any = None
            for tag in ["mixed-citation", "element-citation", "citation"]:
                if len(reference_node_abs.xpath(tag)) > 0:
                    reference_node = reference_node_abs.xpath(tag)[0]
                    break

            if reference_node is None:
                continue

            if all(
                not (ref_type in ["citation-type", "publication-type"])
                for ref_type in reference_node.attrib.keys()
            ):
                continue

            # Author names
            names = []
            if len(reference_node.xpath("name")) > 0:
                for name_node in reference_node.xpath("name"):
                    name_str = " ".join(
                        [
                            t.text
                            for t in name_node.getchildren()
                            if (t.text is not None)
                        ]
                    )
                    names.append(name_str)
            elif len(reference_node.xpath("person-group")) > 0:
                for name_node in reference_node.xpath("person-group")[0]:
                    if name_node.tag == "name":
                        surnames = name_node.xpath("surname")
                        given_names = name_node.xpath("given-names")

                        if len(surnames) > 0 and len(given_names) > 0:
                            given_name = given_names[0].text
                            names.append(
                                surnames[0].text + " " + given_name
                                if given_name
                                else ""
                            )
                    elif name_node.tag == "etal":
                        names.append("et al.")
            reference["author_names"] = "; ".join(names)

            # Title
            if len(reference_node.xpath("article-title")) > 0:
                reference["title"] = " ".join(
                    [
                        t.replace("\n", " ")
                        for t in reference_node.xpath("article-title")[0].itertext()
                    ]
                )

            # Journal
            if len(reference_node.xpath("source")) > 0:
                reference["journal"] = reference_node.xpath("source")[0].text

            # Year
            if len(reference_node.xpath("year")) > 0:
                reference["year"] = reference_node.xpath("year")[0].text

            if (
                not (reference_node.xpath("article-title"))
                and not (reference_node.xpath("journal"))
                and not (reference_node.xpath("year"))
            ):
                reference["title"] = reference_node.text

            references.append(reference)
        return references

    def _add_table(
        self,
        doc: DoclingDocument,
        table_xml_component: Table,
        parent_node: Optional[NodeItem],
    ) -> None:

        label = (
            table_xml_component["label"] if table_xml_component["label"] else "Table"
        )
        caption = (
            table_xml_component["caption"] if table_xml_component["caption"] else ""
        )
        table_caption = doc.add_text(
            label=DocItemLabel.CAPTION, text=label + ": " + caption
        )

        try:
            soup = BeautifulSoup(table_xml_component["content"], "html.parser")
            table_tag = soup.find("table")

            nested_tables = table_tag.find("table")
            if nested_tables:
                _log.debug(f"Skipping nested table for: {str(self.file)}")
                return

            # Count the number of rows (number of <tr> elements)
            num_rows = len(table_tag.find_all("tr"))

            # Find the number of columns (taking into account colspan)
            num_cols = 0
            for row in table_tag.find_all("tr"):
                col_count = 0
                for cell in row.find_all(["td", "th"]):
                    colspan = int(cell.get("colspan", 1))
                    col_count += colspan
                num_cols = max(num_cols, col_count)

            grid = [[None for _ in range(num_cols)] for _ in range(num_rows)]

            data = TableData(num_rows=num_rows, num_cols=num_cols, table_cells=[])

            # Iterate over the rows in the table
            for row_idx, row in enumerate(table_tag.find_all("tr")):
                # For each row, find all the column cells (both <td> and <th>)
                cells = row.find_all(["td", "th"])

                # Check if each cell in the row is a header -> means it is a column header
                col_header = True
                for j, html_cell in enumerate(cells):
                    if html_cell.name == "td":
                        col_header = False

                # Extract and print the text content of each cell
                col_idx = 0
                for _, html_cell in enumerate(cells):
                    text = html_cell.text

                    col_span = int(html_cell.get("colspan", 1))
                    row_span = int(html_cell.get("rowspan", 1))

                    while grid[row_idx][col_idx] is not None:
                        col_idx += 1
                    for r in range(row_span):
                        for c in range(col_span):
                            grid[row_idx + r][col_idx + c] = text

                    cell = TableCell(
                        text=text,
                        row_span=row_span,
                        col_span=col_span,
                        start_row_offset_idx=row_idx,
                        end_row_offset_idx=row_idx + row_span,
                        start_col_offset_idx=col_idx,
                        end_col_offset_idx=col_idx + col_span,
                        column_header=col_header,
                        row_header=((not col_header) and html_cell.name == "th"),
                    )
                    data.table_cells.append(cell)
        except IndexError:
            table_caption.parent = parent_node
            return table_caption

        return doc.add_table(data=data, parent=parent_node, caption=table_caption)

    def _parse_and_populate(self, doc: DoclingDocument):
        title: str = self._parse_title()
        title_node = doc.add_text(
            parent=None,
            text=title,
            label=DocItemLabel.TITLE,
        )

        authors = self._parse_authors()
        authors_affiliations: list = []
        for author in authors:
            authors_affiliations.append(author["name"])
            authors_affiliations.append(", ".join(author["affiliation_names"]))
        authors_affiliations_str = "; ".join(authors_affiliations)

        doc.add_text(
            parent=title_node,
            text=authors_affiliations_str,
            label=DocItemLabel.PARAGRAPH,
        )

        abstract_text = self._parse_abstract()
        abstract_node = doc.add_heading(parent=title_node, text="Abstract")
        doc.add_text(
            parent=abstract_node,
            text=abstract_text,
            label=DocItemLabel.TEXT,
        )

        self._dfs_parse_body(title_node, doc)

        references = self._parse_references()
        references_node = doc.add_heading(parent=title_node, text="References")
        current_list = doc.add_group(
            parent=references_node, label=GroupLabel.LIST, name="list"
        )
        for reference in references:
            reference_text: str = ""
            if reference["author_names"]:
                reference_text += reference["author_names"] + ". "

            if reference["title"]:
                reference_text += reference["title"]
                if reference["title"][-1] != ".":
                    reference_text += "."
                reference_text += " "

            if reference["journal"]:
                reference_text += reference["journal"]

            if reference["year"]:
                reference_text += " (" + reference["year"] + ")"

            if not (reference_text):
                _log.debug(f"Skipping reference for: {str(self.file)}")
                continue

            doc.add_list_item(
                text=reference_text, enumerated=False, parent=current_list
            )

        return doc

    def _dfs_parse_body(self, root_node: NodeItem, doc: DoclingDocument):
        body = self.tree.find("body")

        if body is None:
            raise ValueError("The XML is not correctly formatted.")

        # Initialize stack with root element pairs of (element, parent_doc)
        stack: List[Tuple[etree._Element, Optional[NodeItem]]] = [
            (child, root_node) for child in reversed(body)
        ]

        while stack:
            # Get XML element and its corresponding Node parent
            element, parent_node = stack.pop()

            new_node = self._process_element(element, parent_node, doc)

            # Push children to stack in reverse order to maintain correct DFS order
            if new_node:
                stack.extend(reversed([(child, new_node) for child in element]))

        # raise Exception

    def _process_element(
        self,
        element: etree._Element,
        parent_node: Optional[NodeItem],
        doc: DoclingDocument,
    ):
        """Convert XML element to Node and attach to parent"""
        if element.tag == "p":
            soup = BeautifulSoup(etree.tostring(element), "xml")
            formatted_paragraph = ""
            for p in soup.find_all("p"):
                for elem in p.children:
                    print(f"Elem: {elem}, ({elem.name})")

                    if elem.name is None:  # Plain text
                        formatted_paragraph += elem
                    elif elem.name == "bold":  # Bold
                        formatted_paragraph += f"**{elem.text}**"
                    elif elem.name == "italic":  # Italic
                        formatted_paragraph += f"*{elem.text}*"
                    elif elem.name == "xref":  # Reference (keeps the number as is)
                        formatted_paragraph += f"[{elem.text}]"
                    elif elem.name == "sub":  # Subscript
                        formatted_paragraph += f"<sub>{elem.text}</sub>"
                    elif elem.name == "sup":  # Superscript
                        formatted_paragraph += f"<sup>{elem.text}</sup>"

            # Remove all subelements with tag xref or italic
            for subel in element:
                if subel.tag in ["xref", "italic", "sub", "sup"]:
                    element.remove(subel)

            # print(text)
            # print("==============================")
            # print(formatted_paragraph)
            # raise Exception

            return doc.add_text(
                text=formatted_paragraph,
                parent=parent_node,
                label=DocItemLabel.PARAGRAPH,
            )

        elif element.tag == "table-wrap":
            table: Table = {"label": "", "caption": "", "content": ""}

            # Content
            if len(element.xpath("table")) > 0:
                table_content_node = element.xpath("table")[0]
            elif len(element.xpath("alternatives/table")) > 0:
                table_content_node = element.xpath("alternatives/table")[0]
            else:
                table_content_node = None

            if table_content_node is not None:
                table["content"] = etree.tostring(table_content_node).decode("utf-8")

            # Caption
            if len(element.xpath("caption/p")) > 0:
                caption_node = element.xpath("caption/p")[0]
            elif len(element.xpath("caption/title")) > 0:
                caption_node = element.xpath("caption/title")[0]
            else:
                caption_node = None
            if caption_node is not None:
                table["caption"] = "".join(
                    [t.replace("\n", "") for t in caption_node.itertext()]
                )

            # Label
            if len(element.xpath("label")) > 0:
                table["label"] = element.xpath("label")[0].text

            if table_content_node is not None:
                self._add_table(doc, table, parent_node)
            else:  # Some table are actually a figure
                table_caption = doc.add_text(
                    label=DocItemLabel.CAPTION,
                    text=table["label"] + ": " + table["caption"],
                )

                graphic_el = element.xpath("graphic")
                img_name: str = [
                    value
                    for key, value in graphic_el[0].items()
                    if key.endswith("href")
                ][0]

                fig_ref = ImageRef.from_pil(
                    image=Image.open(Path(self.path.parent, f"{img_name}.jpg")),
                    dpi=50,
                )

                doc.add_picture(
                    parent=parent_node, caption=table_caption, image=fig_ref
                )
            return None

        elif element.tag == "fig":
            figure_caption: FigureCaption = {
                "caption": "",
                "label": "",
            }

            # Label
            if element.xpath("label"):
                figure_caption["label"] = "".join(
                    [t.replace("\n", "") for t in element.xpath("label")[0].itertext()]
                )

            # Caption
            if element.xpath("caption"):
                caption = ""
                for caption_node in element.xpath("caption")[0].getchildren():
                    caption += (
                        "".join([t.replace("\n", "") for t in caption_node.itertext()])
                        + "\n"
                    )
                figure_caption["caption"] = caption

            figure_caption_text = (
                figure_caption["label"] + ": " + figure_caption["caption"].strip()
            )
            fig_caption = doc.add_text(
                label=DocItemLabel.CAPTION, text=figure_caption_text
            )

            graphic_el = element.xpath("graphic")
            if len(graphic_el) > 0:
                img_name: str = [
                    value
                    for key, value in graphic_el[0].items()
                    if key.endswith("href")
                ][0]

                fig_ref = ImageRef.from_pil(
                    image=Image.open(Path(self.path.parent, f"{img_name}.jpg")),
                    dpi=50,
                )
            else:
                fig_ref = None

            doc.add_picture(parent=parent_node, caption=fig_caption, image=fig_ref)
            return None

        elif element.tag == "sec":
            # Get the title tag of the section
            title_el = element.find("title")
            title_label = element.find("label")

            title_label_text = "" if title_label is None else title_label.text
            if title_label is not None:
                element.remove(title_label)

            # Create a header node with the text content of the title tag
            if title_el is not None:
                element.remove(title_el)
                return doc.add_heading(
                    text=f"{title_label_text} {title_el.text}", parent=parent_node
                )

        # elif (
        #     # element.tag == "title"
        #     # or element.tag == "label"
        #     element.tag == "xref"
        #     or element.tag == "italic"
        #     or element.tag == "sup"
        #     or element.tag == "sub"
        #     or element.tag == "ext-link"
        #     or element.tag == "supplementary-material"
        #     or element.tag == "uri"
        #     or element.tag == "bold"
        #     or element.tag == "underline"
        # ):
        #     return None

        else:
            print(f"Skipping tag: {element.tag}")
            raise Exception
            return None  # Skip unknown tags
