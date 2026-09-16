package com.thomaspeoples.wraithbus.network

/**
 * Field-for-field mirror of backend/app.py's JSON shapes. Kept in one file
 * because the shapes themselves come from one place (bus_properties(),
 * route_stops_for_line(), metrics_summary(), metrics_punctuality()) and
 * drifting a second, slightly different copy across files is exactly how a
 * client silently stops parsing a field the server still sends.
 */

data class Bus(
    val busId: String?,
    val line: String?,
    val bearing: Double?,
    val direction: String?,
    val operator: String?,
    val originName: String?,
    val destName: String?,
    val originAimedDep: String?,
    val destAimedArr: String?,
    val remainingKm: Double?,
    val etaIso: String?,
    val etaSource: String?,
    val delayS: Double?,
    val delaySource: String?,
    val status: String?,
    val lat: Double?,
    val lon: Double?,
)

data class EtaModel(
    val slopeSPerKm: Double?,
    val interceptS: Double?,
    val r2: Double?,
    val nObs: Long?,
    val source: String?,
)

data class StopRef(
    val name: String?,
    val locality: String?,
    val atco: String?,
    val timeIso: String?,
    val lat: Double?,
    val lon: Double?,
)

data class NextStopRef(
    val name: String?,
    val locality: String?,
    val atco: String?,
    val etaIso: String?,
    val aimedIso: String?,
    val lat: Double?,
    val lon: Double?,
)

data class BusDetail(
    val bus: Bus,
    val recordedAt: String?,
    val scheduledArrivalIso: String?,
    val predictedArrivalIso: String?,
    val model: EtaModel?,
    val lastStop: StopRef?,
    val nextStop: NextStopRef?,
)

/** /route's FeatureCollection mixes three feature kinds in one list,
 * distinguished by properties.kind on the server -- mirrored here as a
 * sealed class instead of a shared nullable-everything data class. */
sealed class RouteFeature {
    data class RouteLine(
        val coordinates: List<Pair<Double, Double>>, // (lon, lat), in feed order
        val line: String?,
        val operator: String?,
        val direction: String?,
        val dayType: String?,
        val nStops: Int?,
        val source: String?,
        val geometryQuality: String?,
        val mergedSuspect: Boolean,
        val truncated: Boolean,
    ) : RouteFeature()

    data class RouteStop(
        val atco: String?,
        val name: String?,
        val locality: String?,
        val seq: Int?,
        val medianSecsFromOrigin: Double?,
        val lat: Double,
        val lon: Double,
    ) : RouteFeature()

    data class RouteBus(val bus: Bus) : RouteFeature()
}

data class RouteResult(
    val features: List<RouteFeature>,
    /** route_note from a truncated/unknown line, e.g. "no stop sequence
     * known for line 'X'" -- null when the route resolved cleanly. */
    val note: String?,
)

data class SearchMatch(
    val locality: String?,
    val localityNorm: String?,
    val lat: Double?,
    val lon: Double?,
    val nStops: Long?,
)

data class SearchResult(
    val query: String,
    val matches: List<SearchMatch>,
    val lines: List<String>,
    val busIds: List<String>,
)

data class MetricsSummary(
    val feedAgeS: Long?,
    val busesTracked5m: Long?,
    val positions1h: Long?,
    val hoursSinceBuild: Long?,
    val dqIssuesLastBuild: Long?,
)

data class PunctualitySystem(
    val pctOnTime: Double?,
    val medianDelayS: Double?,
)

data class ModelFit(
    val avgR2: Double?,
    val pctWeakFit: Double?,
    val avgSpeedKmh: Double?,
    val nModels: Long?,
)

data class WorstLine(
    val line: String?,
    val nJourneys: Long?,
    val pctOnTime: Double?,
    val medianDelayS: Double?,
    val p90DelayS: Double?,
)

data class PunctualityResult(
    val system: PunctualitySystem?,
    val modelFit: ModelFit?,
    val worstLines: List<WorstLine>,
)
