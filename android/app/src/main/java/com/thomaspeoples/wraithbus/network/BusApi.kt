package com.thomaspeoples.wraithbus.network

import com.thomaspeoples.wraithbus.BuildConfig
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject

/**
 * The only thing that talks to the Flask API. Every function degrades
 * quietly on any failure -- a timeout, a 503, a malformed body -- returning
 * null (or an empty result) rather than throwing, the same "never break the
 * page over one bad query" discipline backend/app.py itself applies. A
 * screen that can't reach the network should show stale or empty data, not
 * crash.
 */
object BusApi {
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .build()

    private suspend fun getBody(path: String, params: Map<String, String> = emptyMap()): String? =
        withContext(Dispatchers.IO) {
            try {
                val builder = BuildConfig.API_BASE_URL.toHttpUrl().newBuilder()
                    .addPathSegments(path.trimStart('/'))
                for ((k, v) in params) builder.addQueryParameter(k, v)
                val request = Request.Builder().url(builder.build()).get().build()
                client.newCall(request).execute().use { resp ->
                    if (resp.isSuccessful) resp.body?.string() else null
                }
            } catch (_: Exception) {
                null
            }
        }

    private fun JSONObject.optDoubleOrNull(key: String): Double? =
        if (has(key) && !isNull(key)) optDouble(key) else null

    private fun JSONObject.optLongOrNull(key: String): Long? =
        if (has(key) && !isNull(key)) optLong(key) else null

    private fun JSONObject.optStringOrNull(key: String): String? =
        if (has(key) && !isNull(key)) optString(key) else null

    private fun busFromProps(props: JSONObject, lat: Double?, lon: Double?) = Bus(
        busId = props.optStringOrNull("bus_id"),
        line = props.optStringOrNull("line"),
        bearing = props.optDoubleOrNull("bearing"),
        direction = props.optStringOrNull("direction"),
        operator = props.optStringOrNull("operator"),
        originName = props.optStringOrNull("origin_name"),
        destName = props.optStringOrNull("dest_name"),
        originAimedDep = props.optStringOrNull("origin_aimed_dep"),
        destAimedArr = props.optStringOrNull("dest_aimed_arr"),
        remainingKm = props.optDoubleOrNull("remaining_km"),
        etaIso = props.optStringOrNull("eta_iso"),
        etaSource = props.optStringOrNull("eta_source"),
        delayS = props.optDoubleOrNull("delay_s"),
        delaySource = props.optStringOrNull("delay_source"),
        status = props.optStringOrNull("status"),
        lat = lat,
        lon = lon,
    )

    /** GET /buses -- a GeoJSON FeatureCollection of live Point features. */
    suspend fun fetchBuses(): List<Bus> {
        val body = getBody("buses") ?: return emptyList()
        return try {
            val features = JSONObject(body).optJSONArray("features") ?: JSONArray()
            (0 until features.length()).mapNotNull { i ->
                val f = features.optJSONObject(i) ?: return@mapNotNull null
                val coords = f.optJSONObject("geometry")?.optJSONArray("coordinates")
                val lon = coords?.optDouble(0)
                val lat = coords?.optDouble(1)
                busFromProps(f.optJSONObject("properties") ?: JSONObject(), lat, lon)
            }
        } catch (_: Exception) {
            emptyList()
        }
    }

    /** GET /bus/{id} -- a flat object, not GeoJSON. 404 (not live) and 400
     * (bad id) both surface as null here; the screen treats "not found" and
     * "couldn't reach it" the same way. */
    suspend fun fetchBusDetail(busId: String): BusDetail? {
        val body = getBody("bus/$busId") ?: return null
        return try {
            val o = JSONObject(body)
            if (o.has("error")) return null
            val bus = busFromProps(o, o.optDoubleOrNull("lat"), o.optDoubleOrNull("lon"))
            val model = o.optJSONObject("model")?.let {
                EtaModel(
                    slopeSPerKm = it.optDoubleOrNull("slope_s_per_km"),
                    interceptS = it.optDoubleOrNull("intercept_s"),
                    r2 = it.optDoubleOrNull("r2"),
                    nObs = it.optLongOrNull("n_obs"),
                    source = it.optStringOrNull("source"),
                )
            }
            val lastStop = o.optJSONObject("last_stop")?.let {
                StopRef(
                    name = it.optStringOrNull("name"),
                    locality = it.optStringOrNull("locality"),
                    atco = it.optStringOrNull("atco"),
                    timeIso = it.optStringOrNull("time_iso"),
                    lat = it.optDoubleOrNull("lat"),
                    lon = it.optDoubleOrNull("lon"),
                )
            }
            val nextStop = o.optJSONObject("next_stop")?.let {
                NextStopRef(
                    name = it.optStringOrNull("name"),
                    locality = it.optStringOrNull("locality"),
                    atco = it.optStringOrNull("atco"),
                    etaIso = it.optStringOrNull("eta_iso"),
                    aimedIso = it.optStringOrNull("aimed_iso"),
                    lat = it.optDoubleOrNull("lat"),
                    lon = it.optDoubleOrNull("lon"),
                )
            }
            BusDetail(
                bus = bus,
                recordedAt = o.optStringOrNull("recorded_at"),
                scheduledArrivalIso = o.optStringOrNull("scheduled_arrival_iso"),
                predictedArrivalIso = o.optStringOrNull("predicted_arrival_iso"),
                model = model,
                lastStop = lastStop,
                nextStop = nextStop,
            )
        } catch (_: Exception) {
            null
        }
    }

    /** GET /route -- a FeatureCollection mixing route/stop/bus features,
     * told apart by properties.kind. */
    suspend fun fetchRoute(
        line: String,
        direction: String? = null,
        dayType: String? = null,
        operator: String? = null,
    ): RouteResult {
        val params = buildMap {
            put("line", line)
            direction?.let { put("direction", it) }
            dayType?.let { put("day_type", it) }
            operator?.let { put("operator", it) }
        }
        val body = getBody("route", params) ?: return RouteResult(emptyList(), null)
        return try {
            val root = JSONObject(body)
            val note = root.optJSONObject("properties")?.optStringOrNull("note")
            val features = root.optJSONArray("features") ?: JSONArray()
            val parsed = (0 until features.length()).mapNotNull { i ->
                val f = features.optJSONObject(i) ?: return@mapNotNull null
                val geom = f.optJSONObject("geometry") ?: return@mapNotNull null
                val props = f.optJSONObject("properties") ?: JSONObject()
                when (props.optStringOrNull("kind")) {
                    "route" -> {
                        val coords = geom.optJSONArray("coordinates") ?: JSONArray()
                        val points = (0 until coords.length()).map { j ->
                            val pt = coords.getJSONArray(j)
                            pt.getDouble(0) to pt.getDouble(1)
                        }
                        RouteFeature.RouteLine(
                            coordinates = points,
                            line = props.optStringOrNull("line"),
                            operator = props.optStringOrNull("operator"),
                            direction = props.optStringOrNull("direction"),
                            dayType = props.optStringOrNull("day_type"),
                            nStops = if (props.has("n_stops")) props.optInt("n_stops") else null,
                            source = props.optStringOrNull("source"),
                            geometryQuality = props.optStringOrNull("geometry_quality"),
                            mergedSuspect = props.optBoolean("merged_suspect", false),
                            truncated = props.optBoolean("truncated", false),
                        )
                    }
                    "stop" -> {
                        val coords = geom.optJSONArray("coordinates") ?: return@mapNotNull null
                        RouteFeature.RouteStop(
                            atco = props.optStringOrNull("atco"),
                            name = props.optStringOrNull("name"),
                            locality = props.optStringOrNull("locality"),
                            seq = if (props.has("seq")) props.optInt("seq") else null,
                            medianSecsFromOrigin = props.optDoubleOrNull("median_secs_from_origin"),
                            lon = coords.getDouble(0),
                            lat = coords.getDouble(1),
                        )
                    }
                    "bus" -> {
                        val coords = geom.optJSONArray("coordinates")
                        RouteFeature.RouteBus(
                            busFromProps(props, coords?.optDouble(1), coords?.optDouble(0))
                        )
                    }
                    else -> null
                }
            }
            RouteResult(parsed, note)
        } catch (_: Exception) {
            RouteResult(emptyList(), null)
        }
    }

    /** GET /search?q= -- empty result (never null) below the server's own
     * 2-character floor, matching /search's own `empty` shortcut. */
    suspend fun search(query: String): SearchResult {
        if (query.trim().length < 2) return SearchResult(query, emptyList(), emptyList(), emptyList())
        val body = getBody("search", mapOf("q" to query))
            ?: return SearchResult(query, emptyList(), emptyList(), emptyList())
        return try {
            val o = JSONObject(body)
            val matches = o.optJSONArray("matches") ?: JSONArray()
            val matchList = (0 until matches.length()).map { i ->
                val m = matches.getJSONObject(i)
                SearchMatch(
                    locality = m.optStringOrNull("locality"),
                    localityNorm = m.optStringOrNull("locality_norm"),
                    lat = m.optDoubleOrNull("lat"),
                    lon = m.optDoubleOrNull("lon"),
                    nStops = m.optLongOrNull("n_stops"),
                )
            }
            val lines = o.optJSONArray("lines") ?: JSONArray()
            val lineList = (0 until lines.length()).map { lines.getString(it) }
            val busIds = o.optJSONArray("bus_ids") ?: JSONArray()
            val busIdList = (0 until busIds.length()).map { busIds.getString(it) }
            SearchResult(o.optString("query", query), matchList, lineList, busIdList)
        } catch (_: Exception) {
            SearchResult(query, emptyList(), emptyList(), emptyList())
        }
    }

    /** GET /metrics/summary -- null on total failure; individual fields are
     * already independently nullable server-side (one bad qdb_exec call
     * blanks that key, not the whole response), so a partial object here
     * is the expected shape, not an error state. */
    suspend fun fetchMetricsSummary(): MetricsSummary? {
        val body = getBody("metrics/summary") ?: return null
        return try {
            val o = JSONObject(body)
            MetricsSummary(
                feedAgeS = o.optLongOrNull("feed_age_s"),
                busesTracked5m = o.optLongOrNull("buses_tracked_5m"),
                positions1h = o.optLongOrNull("positions_1h"),
                hoursSinceBuild = o.optLongOrNull("hours_since_build"),
                dqIssuesLastBuild = o.optLongOrNull("dq_issues_last_build"),
            )
        } catch (_: Exception) {
            null
        }
    }

    /** GET /metrics/punctuality -- 503s (upstream unavailable) surface as
     * null here, same as every other endpoint's failure path. */
    suspend fun fetchPunctuality(): PunctualityResult? {
        val body = getBody("metrics/punctuality") ?: return null
        return try {
            val o = JSONObject(body)
            val system = o.optJSONObject("system")?.let {
                PunctualitySystem(
                    pctOnTime = it.optDoubleOrNull("pct_on_time"),
                    medianDelayS = it.optDoubleOrNull("median_delay_s"),
                )
            }
            val modelFit = o.optJSONObject("model_fit")?.let {
                ModelFit(
                    avgR2 = it.optDoubleOrNull("avg_r2"),
                    pctWeakFit = it.optDoubleOrNull("pct_weak_fit"),
                    avgSpeedKmh = it.optDoubleOrNull("avg_speed_kmh"),
                    nModels = it.optLongOrNull("n_models"),
                )
            }
            val worstLines = o.optJSONArray("worst_lines") ?: JSONArray()
            val worstList = (0 until worstLines.length()).map { i ->
                val w = worstLines.getJSONObject(i)
                WorstLine(
                    line = w.optStringOrNull("line"),
                    nJourneys = w.optLongOrNull("n_journeys"),
                    pctOnTime = w.optDoubleOrNull("pct_on_time"),
                    medianDelayS = w.optDoubleOrNull("median_delay_s"),
                    p90DelayS = w.optDoubleOrNull("p90_delay_s"),
                )
            }
            PunctualityResult(system, modelFit, worstList)
        } catch (_: Exception) {
            null
        }
    }
}
