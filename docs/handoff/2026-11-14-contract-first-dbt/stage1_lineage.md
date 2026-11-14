# Stage 1 lineage trace (read-only). Repo: wraith-bus-ingestor. Date 2026-11-14

Abbrevs: BP=contracts/bus-positions.odcs.yaml, BM=contracts/bus-marts.odcs.yaml, GT=contracts/gtfs-timetable.odcs.yaml, ONT=contracts/wraith-bus-ingestor.ontology.yaml, V=contracts/validate.py.
Model yml paths: BODSM=dbt/models/staging/bods/_bods__models.yml, BODSS=.../_bods__sources.yml, NAPM=dbt/models/staging/naptan/_naptan__models.yml, NAPS=.../_naptan__sources.yml, INT=dbt/models/intermediate/_intermediate__models.yml, MART=dbt/models/marts/_marts__models.yml.

## 0. GENERATED evidence (global)
- Generator: `git show --stat 9b03b7b` lists deleted `dbt/models/intermediate/_generated_line_stops_batches.yml` (3602 lines) and rewrites of BM, GT (new, 229 lines), V, ONT, `_intermediate__models.yml`. The deleted generated yml was generated from line_stops batch config, not from an ODCS contract. The stat output shows the deleted file but no generator script in the truncated list (head -20); I did not list the full stat.
- Search `grep -rniE "generated|do not edit|odcs|contract" dbt/models dbt/macros dbt/dbt_project.yml dbt/tests dbt/build.sh`: no "generated"/"do not edit" header in any current yml or sql. Only "contract" hits are comments (CONTRACT.md s2 refs: NAPM:44, dbt/macros/_macros.yml:82, dbt/macros/locality_norm.sql:7; bus-marts refs: dbt/models/intermediate/int_journey_delays.sql:78, dbt/models/marts/eta_models.sql:168,98) and build.sh contract gate (dbt/build.sh:245-261).
- `grep -n "contract" dbt/dbt_project.yml`: not found. No dbt `contract: enforced` anywhere.
- `git log` on yml paths: only hand commits (ccc06ff, 4deadf1, ebbb988, a04357c, 9b03b7b, 8a9333e, fbea3c1, 0e1bd3e ...). Nothing says generated from a contract.
=> No dbt yml/column is GENERATED. Every row below is HAND-WRITTEN-*, or NO-CONTRACT-ENTRY.

## A. Coverage matrix
Class codes: HM = HAND-WRITTEN-MATCHES-CONTRACT, HD = HAND-WRITTEN-DIVERGES, NC = NO-CONTRACT-ENTRY.
dbt yml carries no data types at all (only tests/descriptions), so "type" comparison is only possible via tests (castable_as) and SQL casts; noted where relevant.

### stg_bods__vehicle_pings (BODSM:4). BM says staging/intermediate are producer-private (BM:7-10); nearest contract is BP bus_positions.
| column | class | contract ref | notes |
|---|---|---|---|
| bus_id (BODSM:15) | HM | BP pos_bus_id (BP:~106) required, PK | not_null matches required; desc "NOT a key, not unique" matches BP desc |
| line (BODSM:22) | HM | BP pos_line required | not_null matches |
| feed_direction (BODSM:28) | HM | BP pos_direction enum | accepted_values same 4 as BP enum; null before 2026-09-12 matches |
| operator (BODSM:40) | HM | BP pos_operator | no not_null, BP optional. OK |
| journey_ref (BODSM:42) | HM | BP pos_journey_ref | optional, null before 2026-09-12 matches |
| origin_ref (BODSM:48) | HD | BP pos_origin_ref | BP says may NOT be an ATCO code (ESTS 'k28'), yml says "joins directly to stg_naptan__stops.atco_code"; BP desc itself stale ("Joins to line_stops.atco_code") |
| dest_ref (BODSM:50) | HM | BP pos_dest_ref | BP has no description |
| origin_name (BODSM:52) | HD | BP pos_origin_name | BP: underscores for spaces, must de-underscore; yml says "free text as operator publishes it" and does not mention underscores |
| dest_name (BODSM:54) | HM | BP pos_dest_name | BP no description |
| ping_ts (BODSM:56) | NC | none (BP timestamp is ingest time, different meaning) | derived: end of 3-min bucket UTC |
| ping_local_ts, ping_date, ping_local_date, hour_of_day, hour_band, day_type | NC | none in BP | derived columns. hour_band/day_type enums equal ONT HourBand/DayType and BM eta enums minus '*' (BM:187-206, 168-186) |
| lat (BODSM:102) | HD | BP pos_lat required, FLOAT | yml not_null OK; yml says GB bbox guaranteed (SQL filter) |
| lon (BODSM:105) | HD | BP pos_lon required, FLOAT | same |
| bearing (BODSM:108) | HD | BP pos_bearing | BP: 0.0 ambiguous (0.0 written when missing); yml silent on ambiguity |
| origin_aimed_dep_ts (BODSM:110) | NC | BP pos_origin_aimed_dep (LONG ms) | renamed + converted to timestamp; BP warns value may belong to completed journey, yml silent |
| dest_aimed_arr_ts (BODSM:112) | NC | BP pos_dest_aimed_arr | same conversion |
| recorded_at | NC | BP pos_recorded_at | selected in SQL (dbt/models/staging/bods/stg_bods__vehicle_pings.sql:147) but NOT in BODSM columns: undocumented, untested column |
| published_line, block_ref | NC | BP pos_published_line, pos_block_ref | not selected in staging; not in source columns_match_set either (see C) |

### stg_naptan__stops (NAPM:4). Nearest contract: GT naptan_stops (raw NaPTAN columns, different physical table in the GTFS duckdb).
| column | class | contract ref | notes |
|---|---|---|---|
| atco_code | NC | GT np_atco ATCOCode, required PK | not_null+unique mirrors PK, but not derived; renamed |
| stop_name | NC | GT np_name CommonName | renamed |
| stop_type | NC | none | accepted_values BCT/BCS/BCQ/BST; GT has no StopType |
| timing_status | NC | none | |
| status | NC | GT np_status Status | GT allows active/inactive/pending; dbt filters to active |
| lat, lon | NC | GT np_lat/np_lon Latitude/Longitude VARCHAR | dbt casts to DOUBLE, not_null; GT declares VARCHAR |
| locality | NC | GT np_locality LocalityName | dbt coalesces LocalityName, Town, ParentLocalityName (NAPM:33-39); GT raw LocalityName only |
| locality_norm | NC | BM loc_norm (BM:294) semantics | same normalisation rule (macro locality_norm) |

### Intermediate (all NC: BM:7-10 says private). Columns overlapping contract vocab noted.
| model.column | class | contract ref | notes |
|---|---|---|---|
| int_pings_segmented_to_journeys.{journey_key, ping_seq, hop_km, cum_km, first_moving_seq, last_moving_seq, journey_n_pings, journey_start_ts, journey_end_ts} | NC | none | INT:21-61. PK (journey_key, ping_seq) stated in desc only; no unique/combo test on it except dbt/tests/assert_segmented_ping_seq_unique.sql |
| int_journeys_shaped_and_directed.{journey_key (PK unique), line, feed_direction, direction, hour_band, day_type, n_pings, start_cum_km, start_ping_seq, end_ping_seq, path_km, straight_km, duration_secs, mean_speed_kmh, dest_aimed_arr_ts} | NC | line/direction/day_type/hour_band parallel BM line_punctuality; direction accepted_values (INT:91-94) = BP/ONT enum | INT:78-125 |
| int_pings_measured_to_journey_end.{journey_key, ping_seq, line, direction, hour_band, day_type, remaining_km, remaining_secs} | NC | none | INT:138-175 |
| int_eta_daily_stats.{stats_date, operator, line, direction, day_type, hour_band, n_obs, avg_km, avg_secs, sxx, sxy, syy} | NC | none | INT:203-235; no unique test on its grain |
| int_journey_delays.{stats_date, journey_key (PK unique), line, direction, day_type, hour_band, delay_s} | NC | feeds BM line_punctuality | INT:256-284; direction not_null; code comment dbt/models/intermediate/int_journey_delays.sql:78 cites bus-marts "direction required/primaryKey" |

### Marts (nearest contract BM)
| model.column | class | contract ref | notes |
|---|---|---|---|
| eta_models.operator (MART:26) | HM | BM eta_operator (BM:121) required PK1 | not_null matches |
| eta_models.line (MART:34) | HM | BM eta_line PK2 | |
| eta_models.direction (MART:37) | HM | BM eta_direction PK3 enum 4+'*' | accepted_values same 5 |
| eta_models.day_type (MART:52) | HM | BM eta_day_type PK4 enum weekday,sat,sun,'*' | same 4 |
| eta_models.hour_band (MART:70) | HM | BM eta_hour_band PK5 enum 5+'*' | same 6 |
| eta_models.slope_s_per_km (MART:83) | HM | BM eta_slope required DOUBLE | not_null matches |
| eta_models.intercept_s (MART:91) | HM | BM eta_intercept required | not_null matches. Ranges differ: yml "300-750 s", BM "400-750 s" (HD on description) |
| eta_models.r2 (MART:99) | HD | BM eta_r2 (not required) | yml not_null, BM not required; BM range 0..1 rule has no dbt test |
| eta_models.n_obs (MART:106) | HM | BM eta_n_obs required BIGINT | not_null; >=200 floor not tested in dbt (only in SQL header, eta_models.sql:109) |
| eta_models.mean_speed_kmh (MART:109) | HD | BM eta_mean_speed (not required) | yml not_null; BM not required but rule speeds_plausible fails on NULL (BM:116-118) so effectively required |
| localities.locality_norm (MART:120) | HM | BM loc_norm required unique PK | not_null+unique |
| localities.locality (MART:123) | HM | BM loc_display required | desc: yml "most common raw form", BM "as NaPTAN spells it" - compatible |
| localities.lat / lon (MART:126,129) | HM | BM loc_lat/loc_lon required DOUBLE | not_null |
| localities.n_stops (MART:132) | HD | BM loc_n_stops (not required, BIGINT) | yml not_null, BM not required |
| line_punctuality.line/direction/day_type/hour_band (MART:152-176) | HM | BM punc_line/direction/day_type/hour_band required PK1-4 | not_null; yml adds accepted_values on direction/day_type/hour_band, BM has no enum on those (yml stricter) |
| line_punctuality.median_delay_s, p90_delay_s, n_journeys (MART:183-189) | HD | BM punc_median/p90/n (not required) | yml not_null on all three, BM not required. yml "n_journeys at least 3" not in BM |

### Sources
| source.table.column | class | contract ref | notes |
|---|---|---|---|
| bods.bus_positions (BODSS:23) | HD | BP bus_positions | external_location glob of extract.py Parquet, not QuestDB table. Description "seven days" (BODSS:20) vs BP usage "rolling 4-day window" (BP:~38) |
| .timestamp (BODSS:43) | HD | BP pos_timestamp | yml: "End of 3-minute sample bucket, UTC (TIMESTAMPTZ on disk)"; BP: "When WE wrote the row (ingest time) TIMESTAMP". Meanings differ by export step |
| .bus_id (50) | HM | BP pos_bus_id | not_null; reuse caveat matches |
| .line (57) | HD | BP pos_line required | yml no not_null though BP required |
| .lat / .lon (59,75) | HD | BP FLOAT required | yml castable_as DOUBLE (BP FLOAT), no not_null (BP required); between-bounds 49.8..61.0 / -8.7..2.0 vs BP bbox -6.3,49.9,1.8,58.75 (BP customProperties boundingBox) |
| .bearing (87) | HM | BP pos_bearing | castable DOUBLE, no 0.0 ambiguity mention |
| .origin_aimed_dep, .dest_aimed_arr (93,101) | HM | BP LONG | castable BIGINT; null-before-2026-09-12 matches |
| .recorded_at (109) | HD | BP pos_recorded_at | yml: NULL in files written before extract carried it (2026-09-20); BP: null before 2026-09-12. BP: only valid freshness signal; yml: tiebreak, "never a time to trust" |
| .direction, .operator, .journey_ref, .origin_ref, .origin_name, .dest_ref, .dest_name | NC (no column entry) | BP pos_* | only in columns_match_set list (BODSS:123), no description/tests |
| (table) columns_match_set (BODSS:121) | HD | BP properties | list has 16 cols; BP has 17 non-timestamp... BP props: bus_id,timestamp,recorded_at,line,lat,lon,bearing,operator,direction,journey_ref,published_line,origin_ref,origin_name,dest_ref,dest_name,origin_aimed_dep,dest_aimed_arr,block_ref (18). Missing from yml list: published_line, block_ref |
| (table) freshness (BODSS:38-41) | HD | BP slaProperties latency 10 m on recorded_at (BP ~280) | yml: loaded_at_field "timestamp", warn 2d / error 5d; a different clock and horizon |
| naptan.stops (NAPS:7) | NC | GT naptan_stops is a different physical table (GTFS duckdb, v GT server gtfs-served); CSV path data/naptan_stops.csv, no columns listed in NAPS | no freshness, no tests |

### GTFS contract objects with no dbt model/source
GT naptan_stops, gtfs_route_patterns, gtfs_route_stops, gtfs_line_alias, gtfs_line_localities: built by gtfs/ (GT header lines 4-6), no dbt counterpart.

## B. Test duplication table
Contract rule (file:rule) | dbt test | Status
| BM eta_model_grain_unique (BM:87) | dbt/tests/assert_eta_model_grain_is_unique.sql | DUPLICATE (same 5 cols) |
| BM eta_model_global_fallback_exists (BM:95) | dbt/tests/assert_eta_model_has_global_fallback.sql | DUPLICATE, dbt slightly weaker (omits operator='*' check; BM includes it; BM mustBe 1, dbt only requires >=1) |
| BM eta_model_speeds_plausible (BM:108) | dbt/tests/assert_eta_model_speeds_are_plausible.sql | DUPLICATE, dbt stricter (also slope<=0, |speed-3600/slope|>1e-6) |
| BM required/PK nullValues on eta_models PK cols | MART not_null tests | DUPLICATE |
| BM eta enum on direction/day_type/hour_band | MART accepted_values (MART:49,67,80) | DUPLICATE (V does not check enum on live data; only ONT/contract enum agreement, V:363-370) |
| BM loc_norm unique/required (V:277-284) | MART localities not_null,unique | DUPLICATE |
| BM loc_lat/lon/locality required | MART not_null | DUPLICATE |
| BM line_punctuality PK required x4 | MART not_null | DUPLICATE |
| BM eta_slope/intercept/n_obs required | MART not_null | DUPLICATE |
| BP bus_id/line/lat/lon/timestamp required | BODSS/BODSM not_null (bus_id; timestamp; stg line/lat/lon) | PARTIAL: source line, lat, lon lack not_null |
| BP pos_direction enum | BODSM:37-39 accepted_values on stg feed_direction | DUPLICATE (on stg, not source) |
| BP physicalType (FLOAT/LONG) | BODSS castable_as DOUBLE/BIGINT | PARTIAL (different types by design) |
| BP property list (18 cols) | BODSS columns_match_set (16) | PARTIAL, missing published_line, block_ref |

Contract rules with NO dbt test:
- BM eta_model_has_rows rowCount >500 (BM:80)
- BM eta_r2_in_range 0..1 (BM:241-247)
- BM eta_n_obs_floor <200 = 0 (BM:256-263) (only enforced in SQL filter, eta_models.sql:109, and header comment)
- BM localities_has_rows >10000 (BM:282); BM localities_contains_known_place WILMSLOW (BM:287)
- BM punctuality_grain_unique (BM:352) (no singular test; int_journey_delays.journey_key unique is upstream only)
- BM loc_norm_no_nulls (covered by not_null; counted as duplicate)
- BM text rules (punctuality_may_be_empty) : not executable (V:176-178)
- BP positions_ingest_is_live count >1000 (BP:~44): ingest-side, not dbt (V runs it on QuestDB)
- BP positions_schedule_capture, positions_no_vehicle_id_column: text, info; no test
- GT all rules (rowCount, grain_unique per table): no dbt model; checked only by V --gtfs
- BP/BM SLA properties (latency, frequency, retention, timeOfAvailability): no dbt test. dbt source freshness is the only SLA-like thing, on different field.

dbt tests with NO contract rule:
- dbt/tests/assert_eta_stats_match_raw_fit.sql, assert_journey_shape_filters_held.sql, assert_segmented_order_is_total.sql, assert_segmented_ping_seq_unique.sql, assert_temporal_buckets_are_local.sql
- macros/tests/schema_drift.sql (castable_as, columns_match_set: generic tests, used by BODSS)
- BODSS expect_compound_columns_to_be_unique (timestamp,bus_id,operator,line) (BODSS:133): BP PK is bus_id (+timestamp partition) only
- BODSS expect_table_row_count_to_be_between min 500000 (BODSS:141); lat/lon between warn tests (BODSS:69,81)
- BODSM hour_band/day_type accepted_values and not_null on stg; all INT yml tests; NAPM tests (unique atco_code, stop_type accepted_values, not_null locality); localities/eta tests as above beyond contract (r2 not_null, mean_speed not_null, n_journeys/median/p90 not_null, line_punctuality enum accepted_values)

## C. Divergence list (schema yml vs contract, today)
1. Source `bods.bus_positions.timestamp` meaning: bucket end UTC TIMESTAMPTZ (BODSS:44) vs BP ingest time TIMESTAMP (BP pos_timestamp); BP says never use for freshness yet BODSS freshness uses it (BODSS:38).
2. Freshness: BODSS warn 2d/error 5d on timestamp vs BP SLA latency 10 m on recorded_at; BM SLA latency 26 h on eta_models (no dbt freshness for marts).
3. columns_match_set (BODSS:123) omits published_line and block_ref present in BP.
4. recorded_at null-boundary: 2026-09-20 (BODSS:112-115) vs 2026-09-12 (BP pos_recorded_at). recorded_at semantic: tiebreak-only (yml) vs sole freshness signal (BP).
5. Source line/lat/lon lack not_null though BP required; lat/lon declared FLOAT in BP, DOUBLE castable in yml; lat/lon bounds in yml (49.8..61.0, -8.7..2.0) wider than BP bbox (-6.3..1.8, 49.9..58.75).
6. Source description "seven days" (BODSS:20) vs BP "rolling 4-day window (EXTRACT_DAYS)"; BP retention 3 d, BM history 8 d.
7. stg.origin_ref desc "joins directly to atco_code" (BODSM:49) vs BP warning it may not be ATCO; BP origin_ref desc references nonexistent line_stops.atco_code (post ADR-0008).
8. stg.origin_name desc omits underscore encoding BP documents; stg.bearing omits 0.0-ambiguity.
9. stg.recorded_at exists in SQL (stg_bods__vehicle_pings.sql:147) but absent from BODSM columns.
10. stg_naptan lat/lon DOUBLE (cast) vs GT naptan_stops Latitude/Longitude VARCHAR; locality coalesce vs raw LocalityName; GT Status allows pending, dbt filters active.
11. Nullability: yml adds not_null where contract has none: eta_models.r2, eta_models.mean_speed_kmh, localities.n_stops, line_punctuality.{median_delay_s,p90_delay_s,n_journeys}.
12. Enums: yml accepted_values on line_punctuality.direction/day_type/hour_band; BM defines none for that object.
13. eta_models.intercept_s range text: yml 300-750 s (MART:94) vs BM 400-750 s (BM:228). BM warns intercept model ~7 min late; yml silent.
14. Global-fallback test omits operator='*' (BM:106 includes it) and tolerates >1 row.
15. BM version 3.1.1 status draft (BM:18-19); BM roles says "five mart tables" (BM:459) but contract defines 3 (eta_models, localities, line_punctuality).
16. BM refresh text says marts rebuilt via dbt/build.sh; ok. BM `eta_models` physicalType VARCHAR etc. not verifiable from yml (no types).
17. Types: yml has no column data_type anywhere, so type/nullability drift can only be caught by V against the built DB, never by dbt contract enforcement (no `contract:` in dbt_project.yml).

## D. Ontology (ONT) vs contracts and V
V checks (V:303-399): (a) valueType is a declared valueType or a primitive (V:334-337); (b) non-computed property must have binding, else fail (V:338-343); (c) binding resolves contract alias/object/property (V:344-360); (d) if both ontology valueType enum and contract property enum exist, ontology enum must be subset of contract enum (V:363-370); (e) primaryKey names exist as properties (V:372-375); titleProperty exists (V:376-379); (f) link from/to objectType exists and referenced property exists (V:381-394). Skipped for contracts not validated that run (V:313-320); build.sh validates only BP and BM (dbt/build.sh:260-261), so GTFS-bound ontology elements are skipped in the build gate.
V does NOT check: type compatibility ontology valueType vs contract physicalType; ontology nullable vs contract required; ontology primaryKey vs contract primaryKey; valueType pattern constraint (AtcoCode, ONT valueTypes); link cardinality vs data; link keys' type agreement; functions (rateVehicle etc., ONT functions block); computed properties; objectType status; backingDatasets (declared but never read by V); linkTypes' `through` datasets.

| Object.property | binds to | V checks |
|---|---|---|
| Vehicle.busId/latitude/longitude/bearing/recordedAt/lineId/operatorRef/journeyRef | BP bus_positions.bus_id/lat/lon/bearing/recorded_at/line/operator/journey_ref | existence only (V:344-361). ONT bearing/operatorRef/journeyRef nullable:true; BP bearing/operator/journey_ref not required: consistent but unchecked. ONT lat/lon have no nullable:true, BP required: consistent, unchecked |
| Vehicle.delaySeconds/delaySource/ragStatus/predictedArrival/remainingKm | computed (no binding) | skipped (V:340-342) |
| Journey.operatorRef/journeyRef/lineId/direction/originRef/destinationRef/originAimedDeparture/destinationAimedArrival | BP operator/journey_ref/line/direction/origin_ref/dest_ref/origin_aimed_dep/dest_aimed_arr | existence; Direction enum subset check applies to direction (V:363-370) |
| Journey.isCircular | computed | skipped |
| Line.lineId | GT gtfs_line_localities.line | existence only if GT validated (skipped in build gate) |
| Operator.operatorRef | BP bus_positions.operator | existence |
| Stop.atcoCode/stopName/localityName/latitude/longitude | GT naptan_stops.ATCOCode/CommonName/LocalityName/Latitude/Longitude | existence if GT validated. GT Latitude/Longitude are VARCHAR; ONT valueType not compared |
| LineStop.operatorRef/lineId/direction/patternId/atcoCode/sequence/scheduledSecondsFromOrigin | GT gtfs_route_stops.operator/line/direction/pattern_id/atco_code/seq/sched_secs_from_origin | existence if GT validated |
| Locality.localityNorm/localityName/latitude/longitude/stopCount | BM localities.locality_norm/locality/lat/lon/n_stops | existence |
| TravelTimeModel.lineId/direction/dayType/hourBand/slope/intercept/rSquared/observations/meanSpeedKmh | BM eta_models.line/direction/day_type/hour_band/slope_s_per_km/intercept_s/r2/n_obs/mean_speed_kmh | existence; enum subset passes (ONT enum lacks '*', BM has it) |
| TravelTimeModel (no operator property) | BM eta_models.operator is PK1 | ONT primaryKey [lineId, direction, dayType, hourBand] omits operator; BM PK has 5 cols. V:372-375 does not compare. DIVERGENCE (ONT drift) |
| PunctualityProfile.* (7 props) | BM line_punctuality.* | existence. status: proposed |
| Links vehicleOperatedBy, vehicleWorking, journeyOnLine, journeyStartsAt, journeyEndsAt, lineCallsAt, lineStopIsStop, lineServesLocality (through GT gtfs_line_localities), lineHasTravelTimeModel, lineHasPunctualityProfile | n/a | V:381-394 checks only end objectTypes/properties exist; lineServesLocality `through` not validated |

Ontology elements with NO dbt counterpart: objects Vehicle, Journey, Line, Operator, Stop, LineStop (BP/GT backed; no dbt model); computed props (delaySeconds, delaySource, ragStatus, predictedArrival, remainingKm, isCircular); functions rateVehicle, predictArrival, searchLocalities, routeGeometry; valueTypes RagStatus, DelaySeconds, EpochMillis, AtcoCode, ServiceNumber (no dbt test), LocalityNorm; links except lineHasTravelTimeModel/lineHasPunctualityProfile/Locality ones are not dbt-backed.
Ontology elements WITH a dbt counterpart: Locality (localities), TravelTimeModel (eta_models), PunctualityProfile (line_punctuality); Direction/DayType/HourBand enums mirror dbt accepted_values lists.
dbt models/columns with no ontology element: all staging and intermediate models, stg_naptan__stops (Stop binds to GT naptan_stops, not dbt), eta_models.operator, stg ping derived columns.
