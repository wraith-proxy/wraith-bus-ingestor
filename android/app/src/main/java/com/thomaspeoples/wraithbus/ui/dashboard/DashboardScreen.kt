package com.thomaspeoples.wraithbus.ui.dashboard

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.thomaspeoples.wraithbus.network.BusApi
import com.thomaspeoples.wraithbus.network.MetricsSummary
import com.thomaspeoples.wraithbus.network.PunctualityResult
import com.thomaspeoples.wraithbus.network.WorstLine
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay

private val StatGreen = Color(0xFF2E7D32)
private val StatYellow = Color(0xFFF9A825)
private val StatRed = Color(0xFFC62828)
private val StatNeutral = Color(0xFF9E9E9E)

private fun feedAgeColor(feedAgeS: Long?): Color = when {
    feedAgeS == null -> StatNeutral
    feedAgeS < 120 -> StatGreen
    feedAgeS <= 900 -> StatYellow
    else -> StatRed
}

private fun dqIssuesColor(dqIssues: Long?): Color =
    if (dqIssues != null && dqIssues > 0) StatRed else StatGreen

private fun onTimePctColor(pctOnTime: Double?): Color = when {
    pctOnTime == null -> StatNeutral
    pctOnTime < 85.0 -> StatRed
    pctOnTime < 95.0 -> StatYellow
    else -> StatGreen
}

private fun r2Color(avgR2: Double?): Color = when {
    avgR2 == null -> StatNeutral
    avgR2 < 0.5 -> StatRed
    avgR2 < 0.7 -> StatYellow
    else -> StatGreen
}

private fun weakFitColor(pctWeakFit: Double?): Color = when {
    pctWeakFit == null -> StatNeutral
    pctWeakFit < 10.0 -> StatGreen
    pctWeakFit < 25.0 -> StatYellow
    else -> StatRed
}

private fun formatSeconds(value: Long?): String = if (value == null) "No data" else "${value}s"

private fun formatHours(value: Long?): String = if (value == null) "—" else "${value}h"

private fun formatCount(value: Long?): String = value?.toString() ?: "—"

private fun formatPct(value: Double?): String = if (value == null) "—" else "${"%.0f".format(value)}%"

private fun formatSignedSeconds(value: Double?): String {
    if (value == null) return "—"
    val rounded = Math.round(value)
    val sign = if (rounded >= 0) "+" else ""
    return "$sign${rounded}s"
}

private fun formatR2(value: Double?): String = if (value == null) "—" else "%.2f".format(value)

private fun formatKmh(value: Double?): String = if (value == null) "—" else "${"%.0f".format(value)} km/h"

@Composable
private fun StatCard(
    label: String,
    value: String,
    modifier: Modifier = Modifier,
    backgroundColor: Color = StatNeutral.copy(alpha = 0.15f),
) {
    Card(
        modifier = modifier,
        colors = CardDefaults.cardColors(containerColor = backgroundColor),
    ) {
        Column(modifier = Modifier.padding(12.dp)) {
            Text(text = label, style = MaterialTheme.typography.labelMedium)
            Text(text = value, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
        }
    }
}

@Composable
private fun WorstLinesTable(worstLines: List<WorstLine>) {
    if (worstLines.isEmpty()) {
        Text(text = "No data yet", style = MaterialTheme.typography.bodyMedium)
        return
    }
    Column(modifier = Modifier.fillMaxWidth()) {
        Row(modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
            Text("Line", modifier = Modifier.weight(1f), fontWeight = FontWeight.Bold)
            Text("Journeys", modifier = Modifier.weight(1f), fontWeight = FontWeight.Bold)
            Text("On-time", modifier = Modifier.weight(1f), fontWeight = FontWeight.Bold)
            Text("Median", modifier = Modifier.weight(1f), fontWeight = FontWeight.Bold)
            Text("P90", modifier = Modifier.weight(1f), fontWeight = FontWeight.Bold)
        }
        worstLines.forEach { line ->
            Row(modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
                Text(line.line ?: "—", modifier = Modifier.weight(1f))
                Text(formatCount(line.nJourneys), modifier = Modifier.weight(1f))
                Text(formatPct(line.pctOnTime), modifier = Modifier.weight(1f))
                Text(formatSignedSeconds(line.medianDelayS), modifier = Modifier.weight(1f))
                Text(formatSignedSeconds(line.p90DelayS), modifier = Modifier.weight(1f))
            }
        }
    }
}

@Composable
fun DashboardScreen() {
    var metrics by remember { mutableStateOf<MetricsSummary?>(null) }
    var punctuality by remember { mutableStateOf<PunctualityResult?>(null) }

    LaunchedEffect(Unit) {
        while (true) {
            coroutineScope {
                val metricsDeferred = async { BusApi.fetchMetricsSummary() }
                val punctualityDeferred = async { BusApi.fetchPunctuality() }
                metrics = metricsDeferred.await()
                punctuality = punctualityDeferred.await()
            }
            delay(60_000)
        }
    }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text(text = "Wraith Bus Platform", style = MaterialTheme.typography.headlineSmall)

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            StatCard(
                label = "Feed age",
                value = formatSeconds(metrics?.feedAgeS),
                modifier = Modifier.weight(1f),
                backgroundColor = feedAgeColor(metrics?.feedAgeS).copy(alpha = 0.2f),
            )
            StatCard(
                label = "Buses tracked",
                value = formatCount(metrics?.busesTracked5m),
                modifier = Modifier.weight(1f),
            )
            StatCard(
                label = "Positions/hour",
                value = formatCount(metrics?.positions1h),
                modifier = Modifier.weight(1f),
            )
        }

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            StatCard(
                label = "Since last build",
                value = formatHours(metrics?.hoursSinceBuild),
                modifier = Modifier.weight(1f),
            )
            StatCard(
                label = "DQ issues",
                value = formatCount(metrics?.dqIssuesLastBuild),
                modifier = Modifier.weight(1f),
                backgroundColor = dqIssuesColor(metrics?.dqIssuesLastBuild).copy(alpha = 0.2f),
            )
        }

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            StatCard(
                label = "System on-time %",
                value = formatPct(punctuality?.system?.pctOnTime),
                modifier = Modifier.weight(1f),
                backgroundColor = onTimePctColor(punctuality?.system?.pctOnTime).copy(alpha = 0.2f),
            )
            StatCard(
                label = "Median delay",
                value = formatSignedSeconds(punctuality?.system?.medianDelayS),
                modifier = Modifier.weight(1f),
            )
        }

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            StatCard(
                label = "Model fit (r²)",
                value = formatR2(punctuality?.modelFit?.avgR2),
                modifier = Modifier.weight(1f),
                backgroundColor = r2Color(punctuality?.modelFit?.avgR2).copy(alpha = 0.2f),
            )
            StatCard(
                label = "Weak model fit %",
                value = formatPct(punctuality?.modelFit?.pctWeakFit),
                modifier = Modifier.weight(1f),
                backgroundColor = weakFitColor(punctuality?.modelFit?.pctWeakFit).copy(alpha = 0.2f),
            )
            StatCard(
                label = "Avg modelled speed",
                value = formatKmh(punctuality?.modelFit?.avgSpeedKmh),
                modifier = Modifier.weight(1f),
            )
        }

        Column {
            Text(text = "Worst lines by delay", style = MaterialTheme.typography.titleMedium)
            WorstLinesTable(worstLines = punctuality?.worstLines ?: emptyList())
        }
    }
}
