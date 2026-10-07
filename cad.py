"""Small, typed native-feature builders. Geometry uses meters; dimensions use explicit mm."""
from copy import deepcopy
import math
import re
from typing import Any

PLANES = {"Top", "Front", "Right"}


def cad_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{24}", value):
        raise ValueError("Document/workspace/element IDs must be 24 lowercase hexadecimal characters.")
    return value


def feature_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Invalid feature ID.")
    return value


def name(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 120 or any(ord(c) < 32 for c in value):
        raise ValueError("Name must contain 1–120 characters without control characters.")
    return value


def dimension(value: float) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or not 0.01 <= value <= 10000:
        raise ValueError("Dimensions must be finite and between 0.01 and 10000 mm.")
    return value


def coordinate(value: float) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or abs(value) > 10000:
        raise ValueError("Coordinates must be finite and within ±10000 mm.")
    return value


def quantity(parameter_id: str, value: float) -> dict[str, Any]:
    return {"btType": "BTMParameterQuantity-147", "parameterId": parameter_id,
            "expression": f"{dimension(value):.12g} mm"}


def string(parameter_id: str, value: str) -> dict[str, Any]:
    return {"btType": "BTMParameterString-149", "parameterId": parameter_id, "value": value}


def enum(parameter_id: str, enum_name: str, value: str) -> dict[str, Any]:
    return {"btType": "BTMParameterEnum-145", "parameterId": parameter_id,
            "enumName": enum_name, "value": value}


def constraint(identifier: str, kind: str, parameters: list[dict]) -> dict:
    return {"btType": "BTMSketchConstraint-2", "entityId": identifier,
            "constraintType": kind, "parameters": parameters}


def sketch_base(sketch_name: str, plane: str) -> dict:
    if plane not in PLANES:
        raise ValueError("Plane must be Top, Front, or Right.")
    return {
        "btType": "BTMSketch-151", "featureType": "newSketch",
        "name": name(sketch_name), "suppressed": False,
        "parameters": [{
            "btType": "BTMParameterQueryList-148", "parameterId": "sketchPlane",
            "queries": [{"btType": "BTMIndividualQuery-138",
                         "queryString": f'query=qCreatedBy(makeId("{plane}"), EntityType.FACE);'}],
        }],
        "entities": [], "constraints": [],
    }


def rectangle(sketch_name: str, plane: str, width_mm: float, height_mm: float,
              x_mm: float = 0, y_mm: float = 0) -> dict:
    """Dimensioned rectangle; anchor bottom-left point with FIX, preserve entity IDs on edits."""
    w, h = dimension(width_mm) / 1000, dimension(height_mm) / 1000
    x, y = coordinate(x_mm) / 1000, coordinate(y_mm) / 1000
    feature = sketch_base(sketch_name, plane)
    vertices = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    ids = ["astra_bottom", "astra_right", "astra_top", "astra_left"]
    for index, entity_id in enumerate(ids):
        start, end = vertices[index], vertices[(index + 1) % 4]
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        feature["entities"].append({
            "btType": "BTMSketchCurveSegment-155", "entityId": entity_id,
            "startPointId": entity_id + ".start", "endPointId": entity_id + ".end",
            "startParam": 0, "endParam": length, "isConstruction": False,
            "geometry": {"btType": "BTCurveGeometryLine-117",
                         "pntX": start[0], "pntY": start[1], "dirX": dx / length, "dirY": dy / length},
        })
        feature["constraints"].extend([
            constraint(f"astra_join_{index}", "COINCIDENT", [
                string("localFirst", entity_id + ".end"),
                string("localSecond", ids[(index + 1) % 4] + ".start"),
            ]),
            constraint(f"astra_orientation_{index}", "HORIZONTAL" if index % 2 == 0 else "VERTICAL",
                       [string("localFirst", entity_id)]),
        ])
    feature["constraints"].append(
        constraint("astra_anchor", "FIX", [string("localFirst", "astra_bottom.start")])
    )
    for label, entity, value in [("width", ids[0], width_mm), ("height", ids[1], height_mm)]:
        feature["constraints"].append(constraint("astra_" + label, "LENGTH", [
            string("localFirst", entity),
            enum("direction", "DimensionDirection", "MINIMUM"),
            quantity("length", value),
            enum("alignment", "DimensionAlignment", "ALIGNED"),
        ]))
    return feature


def circle(sketch_name: str, plane: str, diameter_mm: float,
           x_mm: float = 0, y_mm: float = 0) -> dict:
    feature = sketch_base(sketch_name, plane)
    feature["entities"] = [{
        "btType": "BTMSketchCurve-4", "entityId": "astra_circle",
        "centerId": "astra_circle.center", "isConstruction": False,
        "geometry": {
            "btType": "BTCurveGeometryCircle-115", "radius": dimension(diameter_mm) / 2000,
            "xCenter": coordinate(x_mm) / 1000, "yCenter": coordinate(y_mm) / 1000,
            "xDir": 1, "yDir": 0, "clockwise": False,
        },
    }]
    feature["constraints"] = [
        constraint("astra_anchor", "FIX", [string("localFirst", "astra_circle.center")]),
        constraint("astra_diameter", "DIAMETER", [
            string("localFirst", "astra_circle"), quantity("length", diameter_mm),
        ]),
    ]
    return feature


def extrude(extrude_name: str, sketch_feature_id: str, depth_mm: float,
            opposite_direction: bool = False) -> dict:
    return {
        "btType": "BTMFeature-134", "featureType": "extrude",
        "name": name(extrude_name), "suppressed": False,
        "parameters": [
            enum("bodyType", "ExtendedToolBodyType", "SOLID"),
            enum("operationType", "NewBodyOperationType", "NEW"),
            {"btType": "BTMParameterQueryList-148", "parameterId": "entities",
             "queries": [{"btType": "BTMIndividualSketchRegionQuery-140",
                          "featureId": feature_id(sketch_feature_id)}]},
            enum("endBound", "BoundingType", "BLIND"), quantity("depth", depth_mm),
            {"btType": "BTMParameterBoolean-144", "parameterId": "oppositeDirection",
             "value": opposite_direction},
        ],
    }


def edit_dimension(feature: dict, parameter: str, value_mm: float) -> dict:
    """Preserve all other feature fields. Only recognized bridge dimensions or NEW extrusion depth."""
    result = deepcopy(feature)
    new_expression = quantity("length", value_mm)["expression"]
    if parameter == "depth":
        params = result.get("parameters", [])
        values = {p.get("parameterId"): p.get("value") for p in params}
        if result.get("featureType") != "extrude" or values.get("operationType") != "NEW":
            raise ValueError("Depth editing is restricted to NEW solid extrusions.")
        if values.get("endBound") != "BLIND":
            raise ValueError("Depth editing requires a BLIND extrusion.")
        candidates = [p for p in params if p.get("parameterId") == "depth"]
    else:
        if parameter not in {"width", "height", "diameter"} or result.get("featureType") != "newSketch":
            raise ValueError("Supported parameters: width, height, diameter, depth.")
        constraints = [c for c in result.get("constraints", [])
                       if c.get("entityId") == "astra_" + parameter]
        candidates = [p for c in constraints for p in c.get("parameters", [])
                      if p.get("parameterId") == "length"]
    if len(candidates) != 1:
        raise ValueError("Recognized dimensional parameter not found exactly once; no change made.")
    candidates[0]["expression"] = new_expression
    return result


def feature_payload(feature: dict, current: dict) -> dict:
    source = current.get("sourceMicroversion")
    if not source:
        raise ValueError("Missing sourceMicroversion; refusing an unguarded feature write.")
    return {
        "btType": "BTFeatureDefinitionCall-1406", "feature": feature,
        "sourceMicroversion": source, "rejectMicroversionSkew": True,
        **{k: current[k] for k in ("serializationVersion", "libraryVersion") if k in current},
    }
