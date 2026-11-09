"""DataCite Metadata Schema 4.7: the property catalog and the XML writer."""

import dataclasses
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any


@dataclasses.dataclass(frozen=True)
class PropertySpec:
    """One attribute of the DataCite REST API.

    mandatory: mandatory in DataCite 4.7. A mapping can make more properties mandatory, never fewer.
    write_xml: None for JSON-only attributes.
    """

    name: str
    many: bool
    mandatory: bool
    write_xml: Callable[[ET.Element, Any], None] | None


def catalog() -> dict[str, PropertySpec]:
    """Every property a mapping can use, in XSD element order, which is the order to_xml writes."""
    specs = (
        PropertySpec("doi", many=False, mandatory=True, write_xml=_write_identifier),
        PropertySpec("creators", many=True, mandatory=True, write_xml=_write_creators),
        PropertySpec("titles", many=True, mandatory=True, write_xml=_write_titles),
        PropertySpec("publisher", many=False, mandatory=True, write_xml=_write_publisher),
        PropertySpec("publicationYear", many=False, mandatory=True, write_xml=_write_publication_year),
        PropertySpec("types", many=False, mandatory=True, write_xml=_write_resource_type),
        PropertySpec("subjects", many=True, mandatory=False, write_xml=_write_subjects),
        PropertySpec("contributors", many=True, mandatory=False, write_xml=_write_contributors),
        PropertySpec("dates", many=True, mandatory=False, write_xml=_write_dates),
        PropertySpec("language", many=False, mandatory=False, write_xml=_write_language),
        PropertySpec("alternateIdentifiers", many=True, mandatory=False, write_xml=_write_alternate_identifiers),
        PropertySpec("relatedIdentifiers", many=True, mandatory=False, write_xml=_write_related_identifiers),
        PropertySpec("formats", many=True, mandatory=False, write_xml=_write_formats),
        PropertySpec("version", many=False, mandatory=False, write_xml=_write_version),
        PropertySpec("rightsList", many=True, mandatory=False, write_xml=_write_rights_list),
        PropertySpec("descriptions", many=True, mandatory=False, write_xml=_write_descriptions),
        PropertySpec("fundingReferences", many=True, mandatory=False, write_xml=_write_funding_references),
        PropertySpec("relatedItems", many=True, mandatory=False, write_xml=_write_related_items),
        PropertySpec("url", many=False, mandatory=False, write_xml=None),
        PropertySpec("schemaVersion", many=False, mandatory=False, write_xml=None),
    )
    return {spec.name: spec for spec in specs}


def to_xml(record: dict[str, Any]) -> str:
    """DataCite XML for a record."""
    namespace = "http://datacite.org/schema/kernel-4"
    ET.register_namespace("", namespace)
    ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
    root = ET.Element(f"{{{namespace}}}resource", {
        "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation":
            f"{namespace} https://schema.datacite.org/meta/kernel-4.7/metadata.xsd"})
    for spec in catalog().values():
        if spec.write_xml and spec.name in record:
            spec.write_xml(root, record[spec.name])
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _add(parent: ET.Element, tag: str, text: Any = None, **attributes: Any) -> ET.Element:
    element = ET.SubElement(parent, f"{{http://datacite.org/schema/kernel-4}}{tag}",
                            {key: str(value) for key, value in attributes.items() if value is not None})
    if text is not None:
        element.text = str(text)
    return element


def _add_name(parent: ET.Element, tag: str, entry: dict[str, Any], name_tag: str) -> None:
    element = _add(parent, tag, contributorType=entry.get("contributorType"))
    _add(element, name_tag, entry["name"], nameType=entry.get("nameType"))
    for key in ("givenName", "familyName"):
        if entry.get(key):
            _add(element, key, entry[key])
    for identifier in entry.get("nameIdentifiers", []):
        _add(element, "nameIdentifier", identifier["nameIdentifier"],
             nameIdentifierScheme=identifier["nameIdentifierScheme"], schemeURI=identifier.get("schemeUri"))


def _write_identifier(root: ET.Element, doi: str) -> None:
    _add(root, "identifier", doi, identifierType="DOI")


def _write_creators(root: ET.Element, creators: list[dict[str, Any]]) -> None:
    parent = _add(root, "creators")
    for creator in creators:
        _add_name(parent, "creator", creator, "creatorName")


def _write_titles(root: ET.Element, titles: list[dict[str, str]]) -> None:
    parent = _add(root, "titles")
    for title in titles:
        _add(parent, "title", title["title"])


def _write_publisher(root: ET.Element, publisher: dict[str, str]) -> None:
    _add(root, "publisher", publisher["name"], publisherIdentifier=publisher["publisherIdentifier"],
         publisherIdentifierScheme=publisher["publisherIdentifierScheme"], schemeURI=publisher["schemeUri"])


def _write_publication_year(root: ET.Element, year: str) -> None:
    _add(root, "publicationYear", year)


def _write_resource_type(root: ET.Element, types: dict[str, str]) -> None:
    _add(root, "resourceType", types["resourceType"], resourceTypeGeneral=types["resourceTypeGeneral"])


def _write_subjects(root: ET.Element, subjects: list[dict[str, str]]) -> None:
    parent = _add(root, "subjects")
    for subject in subjects:
        _add(parent, "subject", subject["subject"], subjectScheme=subject.get("subjectScheme"),
             schemeURI=subject.get("schemeUri"), valueURI=subject.get("valueUri"),
             classificationCode=subject.get("classificationCode"))


def _write_contributors(root: ET.Element, contributors: list[dict[str, Any]]) -> None:
    parent = _add(root, "contributors")
    for contributor in contributors:
        _add_name(parent, "contributor", contributor, "contributorName")


def _write_dates(root: ET.Element, dates: list[dict[str, str]]) -> None:
    parent = _add(root, "dates")
    for date in dates:
        _add(parent, "date", date["date"], dateType=date["dateType"], dateInformation=date.get("dateInformation"))


def _write_language(root: ET.Element, language: str) -> None:
    _add(root, "language", language)


def _write_alternate_identifiers(root: ET.Element, identifiers: list[dict[str, str]]) -> None:
    parent = _add(root, "alternateIdentifiers")
    for identifier in identifiers:
        _add(parent, "alternateIdentifier", identifier["alternateIdentifier"],
             alternateIdentifierType=identifier["alternateIdentifierType"])


def _write_related_identifiers(root: ET.Element, identifiers: list[dict[str, str]]) -> None:
    parent = _add(root, "relatedIdentifiers")
    for identifier in identifiers:
        _add(parent, "relatedIdentifier", identifier["relatedIdentifier"],
             relatedIdentifierType=identifier["relatedIdentifierType"], relationType=identifier["relationType"],
             relationTypeInformation=identifier.get("relationTypeInformation"),
             resourceTypeGeneral=identifier.get("resourceTypeGeneral"))


def _write_formats(root: ET.Element, formats: list[str]) -> None:
    parent = _add(root, "formats")
    for value in formats:
        _add(parent, "format", value)


def _write_version(root: ET.Element, version: str) -> None:
    _add(root, "version", version)


def _write_rights_list(root: ET.Element, rights_list: list[dict[str, str]]) -> None:
    parent = _add(root, "rightsList")
    for rights in rights_list:
        _add(parent, "rights", rights["rights"], rightsURI=rights["rightsUri"], rightsIdentifier=rights["rightsIdentifier"],
             rightsIdentifierScheme=rights["rightsIdentifierScheme"], schemeURI=rights["schemeUri"])


def _write_descriptions(root: ET.Element, descriptions: list[dict[str, str]]) -> None:
    parent = _add(root, "descriptions")
    for description in descriptions:
        _add(parent, "description", description["description"], descriptionType=description["descriptionType"])


def _write_funding_references(root: ET.Element, references: list[dict[str, str]]) -> None:
    parent = _add(root, "fundingReferences")
    for reference in references:
        element = _add(parent, "fundingReference")
        _add(element, "funderName", reference["funderName"])
        if reference.get("funderIdentifier"):
            _add(element, "funderIdentifier", reference["funderIdentifier"],
                 funderIdentifierType=reference["funderIdentifierType"], schemeURI=reference.get("schemeUri"))
        if reference.get("awardNumber"):
            _add(element, "awardNumber", reference["awardNumber"])


def _write_related_items(root: ET.Element, items: list[dict[str, Any]]) -> None:
    parent = _add(root, "relatedItems")
    for item in items:
        element = _add(parent, "relatedItem", relatedItemType=item["relatedItemType"], relationType=item["relationType"])
        if item.get("creators"):
            creators = _add(element, "creators")
            for creator in item["creators"]:
                node = _add(creators, "creator")
                _add(node, "creatorName", creator["name"], nameType=creator.get("nameType"))
                for key in ("givenName", "familyName"):
                    if creator.get(key):
                        _add(node, key, creator[key])
        titles = _add(element, "titles")
        for title in item["titles"]:
            _add(titles, "title", title["title"])
        for key in ("publicationYear", "volume", "issue"):
            if item.get(key):
                _add(element, key, item[key])
        if item.get("number"):
            _add(element, "number", item["number"], numberType=item.get("numberType"))
        for key in ("firstPage", "lastPage", "edition"):
            if item.get(key):
                _add(element, key, item[key])
