package com.thomaspeoples.wraithbus.ui.tracking

import android.content.ComponentCallbacks2
import android.content.res.Configuration
import android.graphics.RectF
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.compose.LocalLifecycleOwner
import org.maplibre.geojson.Feature
import org.maplibre.geojson.FeatureCollection
import org.maplibre.geojson.LineString
import org.maplibre.geojson.Point
import com.thomaspeoples.wraithbus.network.Bus
import com.thomaspeoples.wraithbus.network.BusApi
import com.thomaspeoples.wraithbus.network.BusDetail
import com.thomaspeoples.wraithbus.network.RouteFeature
import com.thomaspeoples.wraithbus.network.RouteResult
import com.thomaspeoples.wraithbus.network.SearchResult
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.maplibre.android.camera.CameraPosition
import org.maplibre.android.camera.CameraUpdateFactory
import org.maplibre.android.geometry.LatLng
import org.maplibre.android.geometry.LatLngBounds
import org.maplibre.android.maps.MapLibreMap
import org.maplibre.android.maps.MapView
import org.maplibre.android.maps.Style
import org.maplibre.android.style.expressions.Expression
import org.maplibre.android.style.layers.CircleLayer
import org.maplibre.android.style.layers.LineLayer
import org.maplibre.android.style.layers.PropertyFactory
import org.maplibre.android.style.sources.GeoJsonSource
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/** Exact status palette the web map uses (ghost-site's own MAP_PALETTE.dark),
 * reused here so a bus reads the same colour on phone and browser. */
private val StatusOnTimeColor = Color(0xFF8FA882).toArgb()
private val StatusLateColor = Color(0xFFC96F5E).toArgb()
private val StatusEarlyColor = Color(0xFFD18A3D).toArgb()
private val StatusUnknownColor = Color(0xFF6D7787).toArgb()
// Was 0xFF9AA4B2 -- identical to the web map's old sibStroke value, and
// close enough to style.json's roads-major to disappear. ghost-site's
// MAP_PALETTE.dark.route moved to this same cyan for the same reason;
// keep the two in step.
private val RouteLineColor = Color(0xFF4FD7E8).toArgb()
private val RouteStopColor = Color(0xFF171B21).toArgb()

private const val BUSES_SOURCE_ID = "buses"
private const val BUSES_LAYER_ID = "buses-layer"
private const val ROUTE_SOURCE_ID = "route"
private const val ROUTE_LINE_LAYER_ID = "route-line-layer"
private const val ROUTE_STOP_LAYER_ID = "route-stop-layer"

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TrackingScreen() {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val coroutineScope = rememberCoroutineScope()

    val mapView = remember { MapView(context).apply { onCreate(null) } }

    var maplibreMap by remember { mutableStateOf<MapLibreMap?>(null) }
    var mapStyle by remember { mutableStateOf<Style?>(null) }
    var buses by remember { mutableStateOf<List<Bus>>(emptyList()) }
    var selectedBusDetail by remember { mutableStateOf<BusDetail?>(null) }
    var query by remember { mutableStateOf("") }
    var searchResult by remember { mutableStateOf<SearchResult?>(null) }

    // A MapView never receiving these leaks the GL surface -- addObserver
    // replays the missed onStart/onResume transitions immediately if the
    // host lifecycle is already past them by the time this composes.
    DisposableEffect(lifecycleOwner) {
        val observer = object : DefaultLifecycleObserver {
            override fun onStart(owner: LifecycleOwner) = mapView.onStart()
            override fun onResume(owner: LifecycleOwner) = mapView.onResume()
            override fun onPause(owner: LifecycleOwner) = mapView.onPause()
            override fun onStop(owner: LifecycleOwner) = mapView.onStop()
            override fun onDestroy(owner: LifecycleOwner) = mapView.onDestroy()
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    // onLowMemory has no Lifecycle event of its own -- ComponentCallbacks2
    // is the real channel Android delivers it through.
    DisposableEffect(context) {
        val callbacks = object : ComponentCallbacks2 {
            override fun onConfigurationChanged(newConfig: Configuration) {}
            override fun onLowMemory() {
                mapView.onLowMemory()
            }
            override fun onTrimMemory(level: Int) {}
        }
        context.registerComponentCallbacks(callbacks)
        onDispose { context.unregisterComponentCallbacks(callbacks) }
    }

    LaunchedEffect(mapView) {
        mapView.getMapAsync { map ->
            maplibreMap = map
            map.cameraPosition = CameraPosition.Builder()
                .target(LatLng(53.6, -2.4))
                .zoom(8.0)
                .build()

            map.addOnMapClickListener { point ->
                val screenPoint = map.projection.toScreenLocation(point)
                val hitBox = RectF(
                    screenPoint.x - 24f,
                    screenPoint.y - 24f,
                    screenPoint.x + 24f,
                    screenPoint.y + 24f,
                )
                val hit = map.queryRenderedFeatures(hitBox, BUSES_LAYER_ID).firstOrNull()
                val busId = hit?.takeIf { it.hasProperty("bus_id") }?.getStringProperty("bus_id")
                if (!busId.isNullOrEmpty()) {
                    coroutineScope.launch {
                        selectBus(busId, maplibreMap, mapStyle) { selectedBusDetail = it }
                    }
                }
                true
            }

            map.setStyle(Style.Builder().fromUri("asset://style.json")) { style ->
                style.addSource(GeoJsonSource(BUSES_SOURCE_ID))
                style.addLayer(
                    CircleLayer(BUSES_LAYER_ID, BUSES_SOURCE_ID).withProperties(
                        PropertyFactory.circleRadius(7f),
                        PropertyFactory.circleColor(
                            Expression.match(
                                Expression.get("status"),
                                Expression.color(StatusUnknownColor),
                                Expression.stop("on_time", Expression.color(StatusOnTimeColor)),
                                Expression.stop("late", Expression.color(StatusLateColor)),
                                Expression.stop("early", Expression.color(StatusEarlyColor)),
                            ),
                        ),
                        // The web map's own MAP_PALETTE.dark carries a
                        // 'busStroke' (#12161c) that never made it into this
                        // translation -- without it, StatusUnknownColor
                        // (#6D7787, correctly copied) reads as near-identical
                        // to style.json's own roads-major (#6b7889). 77% of
                        // live buses carry status "unknown" (confirmed
                        // against the real feed), so most dots were
                        // camouflaged against major roads, not missing.
                        PropertyFactory.circleStrokeWidth(1.5f),
                        PropertyFactory.circleStrokeColor(Color(0xFF12161C).toArgb()),
                    ),
                )
                mapStyle = style
            }
        }
    }

    // Matches the web map's own 60s refresh and the ingestor's poll cadence.
    LaunchedEffect(Unit) {
        while (true) {
            buses = BusApi.fetchBuses()
            delay(60_000)
        }
    }

    // Keyed on both so a style that finishes loading after the first fetch
    // (or a fetch that lands before the style is ready) still converges.
    LaunchedEffect(buses, mapStyle) {
        val source = mapStyle?.getSource(BUSES_SOURCE_ID) as? GeoJsonSource ?: return@LaunchedEffect
        val features = buses.mapNotNull { bus ->
            val lat = bus.lat ?: return@mapNotNull null
            val lon = bus.lon ?: return@mapNotNull null
            Feature.fromGeometry(Point.fromLngLat(lon, lat)).apply {
                addStringProperty("status", bus.status ?: "unknown")
                addStringProperty("bus_id", bus.busId ?: "")
            }
        }
        // setGeoJson(), not setGeoJsonSync(): the live feed runs to ~18k
        // features GB-wide, and parsing that synchronously on the main
        // thread (LaunchedEffect runs on Dispatchers.Main) every 60s would
        // jank the UI. setGeoJson() parses off-thread and applies once
        // ready -- the right tradeoff for a dataset this size.
        source.setGeoJson(FeatureCollection.fromFeatures(features))
    }

    LaunchedEffect(query) {
        val trimmed = query.trim()
        if (trimmed.length < 2) {
            searchResult = null
            return@LaunchedEffect
        }
        delay(400)
        searchResult = BusApi.search(trimmed)
    }

    Box(modifier = Modifier.fillMaxSize()) {
        AndroidView(factory = { mapView }, modifier = Modifier.fillMaxSize())

        Column(modifier = Modifier.fillMaxWidth().padding(12.dp)) {
            OutlinedTextField(
                value = query,
                onValueChange = { query = it },
                modifier = Modifier.fillMaxWidth(),
                placeholder = { Text("Search locality, line or bus") },
                singleLine = true,
            )
            val result = searchResult
            val hasResults = result != null &&
                (result.matches.isNotEmpty() || result.lines.isNotEmpty() || result.busIds.isNotEmpty())
            if (result != null && hasResults) {
                Card(modifier = Modifier.fillMaxWidth().padding(top = 4.dp)) {
                    Column {
                        result.lines.forEach { line ->
                            Text(
                                text = "Line $line",
                                style = MaterialTheme.typography.bodyMedium,
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clickable {
                                        coroutineScope.launch {
                                            val route = BusApi.fetchRoute(line = line)
                                            if (route.features.isNotEmpty()) {
                                                fitCameraToRoute(maplibreMap, applyRouteOverlay(mapStyle, route))
                                            }
                                        }
                                        query = ""
                                        searchResult = null
                                    }
                                    .padding(12.dp),
                            )
                        }
                        result.busIds.forEach { busId ->
                            Text(
                                text = "Bus $busId",
                                style = MaterialTheme.typography.bodyMedium,
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clickable {
                                        coroutineScope.launch {
                                            selectBus(busId, maplibreMap, mapStyle) { selectedBusDetail = it }
                                        }
                                        query = ""
                                        searchResult = null
                                    }
                                    .padding(12.dp),
                            )
                        }
                        result.matches.forEach { match ->
                            Text(
                                text = match.locality ?: "-",
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clickable {
                                        val lat = match.lat
                                        val lon = match.lon
                                        if (lat != null && lon != null) {
                                            maplibreMap?.animateCamera(
                                                CameraUpdateFactory.newLatLngZoom(LatLng(lat, lon), 13.0),
                                            )
                                        }
                                        query = ""
                                        searchResult = null
                                    }
                                    .padding(12.dp),
                            )
                        }
                    }
                }
            }
        }
    }

    val detail = selectedBusDetail
    if (detail != null) {
        ModalBottomSheet(onDismissRequest = { selectedBusDetail = null }) {
            BusDetailSheetContent(detail = detail)
        }
    }
}

/** Shared by a map tap on a bus dot and a bus-number search hit: draw the
 * route first (so it's already on the map by the time the sheet covers the
 * bottom half of the screen), fit the camera to it, then open the sheet. */
private suspend fun selectBus(
    busId: String,
    map: MapLibreMap?,
    style: Style?,
    onDetail: (BusDetail?) -> Unit,
) {
    val detail = BusApi.fetchBusDetail(busId) ?: run { onDetail(null); return }
    val line = detail.bus.line
    if (line != null) {
        val route = BusApi.fetchRoute(line = line, direction = detail.bus.direction)
        if (route.features.isNotEmpty()) {
            fitCameraToRoute(map, applyRouteOverlay(style, route))
        }
    }
    onDetail(detail)
}

private fun fitCameraToRoute(map: MapLibreMap?, bounds: LatLngBounds?) {
    if (map == null || bounds == null) return
    map.animateCamera(CameraUpdateFactory.newLatLngBounds(bounds, 64))
}

private val timeFormatter = DateTimeFormatter.ofPattern("HH:mm")

/** Server times are all ISO-8601 UTC ("2026-09-16T18:57:39Z") -- riders
 * want a clock time in their own zone, not a data engineer's timestamp. */
private fun isoToLocalTime(iso: String?): String {
    if (iso.isNullOrBlank()) return "-"
    return try {
        Instant.parse(iso).atZone(ZoneId.systemDefault()).format(timeFormatter)
    } catch (_: Exception) {
        iso
    }
}

@Composable
private fun BusDetailSheetContent(detail: BusDetail) {
    val bus = detail.bus
    Column(modifier = Modifier.fillMaxWidth().padding(16.dp)) {
        Text(text = bus.line ?: "Unknown line", style = MaterialTheme.typography.titleLarge)
        Text(text = bus.operator ?: "-", style = MaterialTheme.typography.bodyMedium)
        Spacer(modifier = Modifier.height(8.dp))
        Text(text = "Direction: ${bus.direction ?: "-"}")
        Text(text = "${bus.originName ?: "-"} -> ${bus.destName ?: "-"}")
        Text(text = "Status: ${bus.status ?: "unknown"}")
        val delayText = formatDelay(bus)
        if (delayText.isNotBlank()) {
            Text(text = delayText)
        }
        bus.etaIso?.let { Text(text = "ETA: ${isoToLocalTime(it)}") }
        detail.predictedArrivalIso?.let { Text(text = "Predicted arrival: ${isoToLocalTime(it)}") }
        detail.nextStop?.let { next ->
            Spacer(modifier = Modifier.height(8.dp))
            Text(text = "Next stop: ${next.name ?: "-"}")
            next.etaIso?.let { Text(text = "Next stop ETA: ${isoToLocalTime(it)}") }
        }
        Spacer(modifier = Modifier.height(16.dp))
    }
}

private fun formatDelay(bus: Bus): String {
    val delaySeconds = bus.delayS
    return when (bus.status) {
        "on_time" -> "on time"
        "late" -> delaySeconds?.let { "${formatMinutesSeconds(it)} late" } ?: ""
        "early" -> delaySeconds?.let { "${formatMinutesSeconds(it)} early" } ?: ""
        else -> ""
    }
}

private fun formatMinutesSeconds(seconds: Double): String {
    val total = kotlin.math.abs(seconds).toLong()
    return "${total / 60}m ${total % 60}s"
}

/** Draws the selected bus's route on top of the live "buses" layer -- one
 * shared GeoJSON source carrying both the line and its stops, since a
 * LineLayer and a CircleLayer over the same source each only render the
 * geometry type they understand. Returns the route's bounds so the caller
 * can fit the camera to it -- a route is useless to look at if it's off
 * screen when the sheet slides up over it. */
private fun applyRouteOverlay(style: Style?, route: RouteResult): LatLngBounds? {
    if (style == null) return null

    val allCoords = route.features.filterIsInstance<RouteFeature.RouteLine>()
        .flatMap { it.coordinates }
    val lineFeatures = route.features.filterIsInstance<RouteFeature.RouteLine>().map { line ->
        Feature.fromGeometry(
            LineString.fromLngLats(line.coordinates.map { (lon, lat) -> Point.fromLngLat(lon, lat) }),
        )
    }
    val stopFeatures = route.features.filterIsInstance<RouteFeature.RouteStop>().map { stop ->
        Feature.fromGeometry(Point.fromLngLat(stop.lon, stop.lat))
    }
    val collection = FeatureCollection.fromFeatures(lineFeatures + stopFeatures)
    val bounds = if (allCoords.isEmpty()) null else LatLngBounds.Builder()
        .apply { allCoords.forEach { (lon, lat) -> include(LatLng(lat, lon)) } }
        .build()

    val existingSource = style.getSource(ROUTE_SOURCE_ID) as? GeoJsonSource
    if (existingSource != null) {
        existingSource.setGeoJson(collection)
        return bounds
    }

    style.addSource(GeoJsonSource(ROUTE_SOURCE_ID, collection))
    val lineLayer = LineLayer(ROUTE_LINE_LAYER_ID, ROUTE_SOURCE_ID).withProperties(
        PropertyFactory.lineColor(RouteLineColor),
        PropertyFactory.lineWidth(4f),
        PropertyFactory.lineOpacity(1f),
        PropertyFactory.lineCap(org.maplibre.android.style.layers.Property.LINE_CAP_ROUND),
        PropertyFactory.lineJoin(org.maplibre.android.style.layers.Property.LINE_JOIN_ROUND),
    )
    val stopLayer = CircleLayer(ROUTE_STOP_LAYER_ID, ROUTE_SOURCE_ID).withProperties(
        PropertyFactory.circleRadius(3f),
        PropertyFactory.circleColor(RouteStopColor),
    )
    // Keep live bus dots on top of the route overlay when it already exists.
    if (style.getLayer(BUSES_LAYER_ID) != null) {
        style.addLayerBelow(lineLayer, BUSES_LAYER_ID)
        style.addLayerBelow(stopLayer, BUSES_LAYER_ID)
    } else {
        style.addLayer(lineLayer)
        style.addLayer(stopLayer)
    }
    return bounds
}
