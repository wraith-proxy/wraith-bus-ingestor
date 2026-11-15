*[Position report]: One raw row from the live feed: where a vehicle says it is, polled about every 60 seconds
*[position report]: One raw row from the live feed: where a vehicle says it is, polled about every 60 seconds
*[Ping]: One vehicle's usable position in a 3-minute bucket; the unit segmentation works on
*[Vehicle]: A physical bus, identified by operator plus bus id
*[Operator]: The company running a vehicle, identified by its National Operator Code. Pooled estimates use * for "all operators"; rows from before the operator field existed form their own unnamed stream
*[Line]: The service identity painted on the bus, such as "42"
*[Pattern]: An ordered sequence of stops that a line follows in the published timetable. Pattern ids are not stable across timetable refreshes
*[Trip]: One scheduled run of a pattern in the published timetable
*[Journey]: An operator's working of a line on a given day, identified by the operator's journey reference plus origin and aimed departure
*[Published timetable]: The operators' timetables as published to the national open data service
*[published timetable]: The operators' timetables as published to the national open data service
*[Inferred route]: The legacy approach of deriving stop sequences from observed pings. Retired in favour of published timetables
*[inferred route]: The legacy approach of deriving stop sequences from observed pings. Retired in favour of published timetables
*[Delay]: The signed live measurement of how late (positive) or early (negative) a vehicle is. Null where progress cannot be computed
*[Journey-end lateness]: How late a journey was when it finished; the measurement behind the punctuality mart
*[journey-end lateness]: How late a journey was when it finished; the measurement behind the punctuality mart
*[ETA residual]: The error of the arrival-time regression against what was observed. It never colours the map
*[Hot window]: The days of raw positions kept on the fast disk in the live database (3 days)
*[hot window]: The days of raw positions kept on the fast disk in the live database (3 days)
*[Extract window]: The days of raw positions exported nightly to Parquet for the build (4 days)
*[extract window]: The days of raw positions exported nightly to Parquet for the build (4 days)
*[History window]: The days of per-day summaries kept after raw positions age out (8 days)
*[history window]: The days of per-day summaries kept after raw positions age out (8 days)
*[Archive]: Cold Parquet copies of old days on the bulk disk. "Glacier" is informal slang for the same cold storage
*[Data product]: A governed dataset with a contract. Four kinds, in dependency order: raw (RDP), origin (ODP), consumption (CDP), functional (FDP). Not every flow uses all four
*[data product]: A governed dataset with a contract. Four kinds, in dependency order: raw (RDP), origin (ODP), consumption (CDP), functional (FDP). Not every flow uses all four
*[RDP]: Data exactly as received from the source
*[Raw data product]: Data exactly as received from the source
*[raw data product]: Data exactly as received from the source
*[ODP]: Data cleaned and typed from the raw source, still shaped like the source
*[Origin data product]: Data cleaned and typed from the raw source, still shaped like the source
*[origin data product]: Data cleaned and typed from the raw source, still shaped like the source
*[CDP]: The staging layer: typed, filtered, per-row-derived tables built from the raw extract, which everything downstream reads
*[Consumption data product]: The staging layer: typed, filtered, per-row-derived tables built from the raw extract, which everything downstream reads
*[consumption data product]: The staging layer: typed, filtered, per-row-derived tables built from the raw extract, which everything downstream reads
*[FDP]: The presentation layers above staging: the intermediate models and the marts the API reads. "Mart" is accepted shorthand for the final, API-facing FDP tables
*[Functional data product]: The presentation layers above staging: the intermediate models and the marts the API reads. "Mart" is accepted shorthand for the final, API-facing FDP tables
*[functional data product]: The presentation layers above staging: the intermediate models and the marts the API reads. "Mart" is accepted shorthand for the final, API-facing FDP tables
