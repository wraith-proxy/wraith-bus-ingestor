"""marshmallow schemas for flask-smorest, one per HTTP response/query shape.

These exist purely to generate an accurate OpenAPI spec + Swagger UI from
the real route decorators (see app.py's Api/Blueprint setup) -- they do not
change any response's actual JSON. Two deliberate exceptions to "model every
field exactly":

- GeoJSON `properties` on a Feature varies by `kind` (route/stop/bus) --
  BusFeaturePropertiesSchema documents the richest (bus) shape; route/stop
  features carry a strict SUBSET of it. Modelling all three as one big
  oneOf was judged not worth the complexity for an internal API with two
  known consumers.
- /buses itself is intentionally NOT schema-validated at response time (see
  app.py) -- it serves a pre-rendered, cached JSON string for performance,
  and running that through marshmallow's dump on every request would defeat
  the point of caching it. Its shape is documented via BusesResponseSchema
  for Swagger UI only.
"""

from marshmallow import Schema, fields


class ApiErrorSchema(Schema):
    error = fields.String(required=True)


# ---------------------------------------------------------------------------
# Shared: a bus, and the pieces of a bus, in the several places they appear
# ---------------------------------------------------------------------------

class BusFeaturePropertiesSchema(Schema):
    """bus_properties()'s exact field set -- what every 'bus' Feature on
    /buses and /route carries, and the base of BusDetailSchema below."""
    bus_id = fields.String(allow_none=True)
    line = fields.String(allow_none=True)
    bearing = fields.Float(allow_none=True)
    direction = fields.String(allow_none=True)
    operator = fields.String(allow_none=True)
    origin_name = fields.String(allow_none=True)
    dest_name = fields.String(allow_none=True)
    origin_aimed_dep = fields.String(allow_none=True, metadata={"format": "date-time"})
    dest_aimed_arr = fields.String(allow_none=True, metadata={"format": "date-time"})
    remaining_km = fields.Float(allow_none=True)
    eta_iso = fields.String(allow_none=True, metadata={"format": "date-time"})
    eta_source = fields.String(
        allow_none=True,
        metadata={"description": "regression | timetable_pinned | timetable_delay | "
                                  "timetable_raw | last_known | arrived | null"})
    delay_s = fields.Integer(allow_none=True)
    delay_source = fields.String(allow_none=True)
    status = fields.String(metadata={"description": "early | on_time | late | unknown"})


class ModelFitSchema(Schema):
    slope_s_per_km = fields.Float(allow_none=True)
    intercept_s = fields.Float(allow_none=True)
    r2 = fields.Float(allow_none=True)
    n_obs = fields.Integer(allow_none=True)
    source = fields.String(allow_none=True)


class StopRefSchema(Schema):
    name = fields.String(allow_none=True)
    locality = fields.String(allow_none=True)
    atco = fields.String(allow_none=True)
    lat = fields.Float(allow_none=True)
    lon = fields.Float(allow_none=True)


class LastStopSchema(StopRefSchema):
    time_iso = fields.String(allow_none=True, metadata={"format": "date-time"})


class NextStopSchema(StopRefSchema):
    eta_iso = fields.String(allow_none=True, metadata={"format": "date-time"})
    aimed_iso = fields.String(allow_none=True, metadata={"format": "date-time"})


class NamesakeSchema(Schema):
    operator = fields.String(allow_none=True)
    line = fields.String(allow_none=True)


class BusDetailSchema(BusFeaturePropertiesSchema):
    """GET /bus/<bus_id>. Extends the properties every bus Feature carries
    with the position and route detail only a single-bus lookup computes."""
    lat = fields.Float(allow_none=True)
    lon = fields.Float(allow_none=True)
    recorded_at = fields.String(allow_none=True, metadata={"format": "date-time"})
    scheduled_arrival_iso = fields.String(allow_none=True, metadata={"format": "date-time"})
    predicted_arrival_iso = fields.String(allow_none=True, metadata={"format": "date-time"})
    model = fields.Nested(ModelFitSchema, allow_none=True)
    last_stop = fields.Nested(LastStopSchema, allow_none=True)
    next_stop = fields.Nested(NextStopSchema, allow_none=True)
    route_source = fields.String(allow_none=True)
    same_bus_id = fields.List(fields.Nested(NamesakeSchema), required=False,
                               metadata={"description": "Present only when bus_id is "
                                                         "ambiguous across operators."})


class BusDetailQuerySchema(Schema):
    operator = fields.String(required=False, metadata={
        "description": "NOC to disambiguate a bus_id shared by more than one operator."})


# ---------------------------------------------------------------------------
# GeoJSON, loosely typed on purpose -- see the module docstring
# ---------------------------------------------------------------------------

class GeometrySchema(Schema):
    type = fields.String(metadata={"description": "'Point' or 'LineString'"})
    coordinates = fields.Raw(metadata={
        "description": "[lon, lat] for a Point; [[lon, lat], ...] for a LineString."})


class FeatureSchema(Schema):
    type = fields.String(dump_default="Feature")
    geometry = fields.Nested(GeometrySchema)
    properties = fields.Dict(metadata={
        "description": "Shape depends on properties.kind on /route ('route' | "
                        "'stop' | 'bus'), or is always the bus shape on /buses -- "
                        "see BusFeaturePropertiesSchema for the richest (bus) case."})


class BusesResponseSchema(Schema):
    """Documents /buses; not used to serialize it -- see the module docstring."""
    type = fields.String(dump_default="FeatureCollection")
    features = fields.List(fields.Nested(FeatureSchema))


class RouteResponseSchema(Schema):
    type = fields.String(dump_default="FeatureCollection")
    features = fields.List(fields.Nested(FeatureSchema))
    properties = fields.Dict(required=False, metadata={
        "description": "Present only as {'note': <string>} when the route is "
                        "degraded (unknown line, or truncated for being suspiciously long)."})


class RouteQuerySchema(Schema):
    # required=False + load_default='', not required=True: a MISSING line
    # must fail through the existing LINE_RE check (a 400 with the existing
    # {"error": "invalid line"} shape), not through webargs' own 422/error
    # shape, which would be a real behaviour change for that one case.
    line = fields.String(required=False, load_default='',
                          metadata={"description": "Published service number."})
    direction = fields.String(required=False, metadata={
        "description": "inbound | outbound | clockwise | anticlockwise"})
    day_type = fields.String(required=False, metadata={
        "description": "Accepted for compatibility; currently ignored -- "
                        "patterns are not day-specific."})
    operator = fields.String(required=False, metadata={"description": "NOC."})


# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------

class SearchMatchSchema(Schema):
    locality = fields.String(allow_none=True)
    locality_norm = fields.String()
    lat = fields.Float(allow_none=True)
    lon = fields.Float(allow_none=True)
    n_stops = fields.Integer(allow_none=True)


class SearchResponseSchema(Schema):
    query = fields.String()
    matches = fields.List(fields.Nested(SearchMatchSchema))
    lines = fields.List(fields.String())
    bus_ids = fields.List(fields.String())


class SearchQuerySchema(Schema):
    q = fields.String(required=False, load_default='', metadata={
        "description": "Free-text locality or line search. Fewer than 2 "
                        "normalised characters returns an empty result."})


# ---------------------------------------------------------------------------
# /model-info, /metrics/*
# ---------------------------------------------------------------------------

class ModelInfoResponseSchema(Schema):
    built_at = fields.String(allow_none=True, metadata={"format": "date-time"})
    n_models = fields.Integer(allow_none=True)
    duckdb = fields.Boolean()
    day_types = fields.List(fields.String())
    marts_state = fields.String(allow_none=True, metadata={
        "description": "ok | locked | missing | unreadable | no_driver"})
    refresh_failed = fields.String(allow_none=True)
    age_hours = fields.Float(allow_none=True)
    stale = fields.Boolean()
    note = fields.String(allow_none=True)


class MetricsSummaryResponseSchema(Schema):
    feed_age_s = fields.Integer(allow_none=True)
    buses_tracked_5m = fields.Integer(allow_none=True)
    positions_1h = fields.Integer(allow_none=True)
    hours_since_build = fields.Integer(allow_none=True)
    dq_issues_last_build = fields.Integer(allow_none=True)


class PunctualitySystemSchema(Schema):
    pct_on_time = fields.Float(allow_none=True)
    median_delay_s = fields.Float(allow_none=True)


class PunctualityModelFitSchema(Schema):
    avg_r2 = fields.Float(allow_none=True)
    pct_weak_fit = fields.Float(allow_none=True)
    avg_speed_kmh = fields.Float(allow_none=True)
    n_models = fields.Integer(allow_none=True)


class WorstLineSchema(Schema):
    line = fields.String(allow_none=True)
    n_journeys = fields.Integer(allow_none=True)
    pct_on_time = fields.Float(allow_none=True)
    median_delay_s = fields.Float(allow_none=True)
    p90_delay_s = fields.Float(allow_none=True)


class MetricsPunctualityResponseSchema(Schema):
    system = fields.Nested(PunctualitySystemSchema, allow_none=True)
    model_fit = fields.Nested(PunctualityModelFitSchema, allow_none=True)
    worst_lines = fields.List(fields.Nested(WorstLineSchema))
