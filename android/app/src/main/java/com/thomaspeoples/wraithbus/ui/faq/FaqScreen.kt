package com.thomaspeoples.wraithbus.ui.faq

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ExpandLess
import androidx.compose.material.icons.filled.ExpandMore
import androidx.compose.material3.Card
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

private val faqEntries: List<Pair<String, String>> = listOf(
    "What is Wraith Bus?" to
        "Wraith Bus is a live UK bus tracker with two main screens: Tracking, which shows a map " +
            "of live bus positions, and Dashboard, which shows pipeline health and punctuality stats.",
    "Where does the bus data come from?" to
        "Positions come from the UK Bus Open Data Service (BODS), a UK Department for Transport " +
            "service. This app polls its SIRI-VM live vehicle position feed every 60 seconds.",
    "Why do so many buses show \"unknown\" status?" to
        "A bus is on_time, late, early, or unknown. Unknown means there isn't enough schedule or " +
            "model data yet to classify that vehicle, not that something is broken. At any given " +
            "moment roughly three-quarters of live buses fall into this category.",
    "Does the app track my location?" to
        "No. Wraith Bus never requests or uses your device's location. It only ever displays where " +
            "buses are, not where you are.",
    "Why isn't this on the Play Store, and how do I get updates?" to
        "Wraith Bus isn't distributed through the Play Store. You sideload it directly, or install " +
            "it via F-Droid (a self-hosted personal repo, not the official public catalog) or " +
            "Obtainium, which tracks the project's Gitea Releases page directly.",
    "What are the notifications for, and can I turn them off?" to
        "An optional foreground service holds a WebSocket to a self-hosted ntfy topic and turns " +
            "incoming pipeline alerts, such as the feed going stale or a nightly data build failing, " +
            "into Android notifications. These are not bus-arrival alerts, and you can disable them " +
            "at any time through the app's standard Android notification settings.",
    "Can I point this app at a different server?" to
        "No. The backend API base URL is baked into the app at build time, so this isn't a " +
            "general-purpose client you can repoint. It's tied to one self-hosted backend as part " +
            "of a single deployed stack.",
    "Is this open source? Where's the code?" to
        "Yes, under the MIT license. The full stack, ingestor, database, API, and this Android app, " +
            "is one open-source project hosted at a self-hosted Gitea instance: " +
            "git.thomaspeoples.com/thomaspeoples/wraith-bus-ingestor.",
    "Where do the map tiles come from?" to
        "Map tiles use OpenStreetMap data, self-hosted and served by the same backend this app " +
            "talks to. Per OSM's license, the map credits \"© OpenStreetMap contributors.\"",
    "Is the bus data always accurate?" to
        "Mostly, but BODS is a live feed from many operators and occasionally contains data quality " +
            "artifacts, such as a route briefly reporting coordinates far outside Great Britain. " +
            "This is a known quirk of the upstream data, not an error in this app.",
    "Do I need an account to use this app?" to
        "No. Wraith Bus has no user accounts, no login, and no analytics or tracking of the person " +
            "using it. You open it and it shows buses.",
    "How does the update check work?" to
        "On launch, the app checks the Gitea Releases page for a newer android-X.Y.Z-debug tag and " +
            "shows a dialog if one is found. This is a courtesy for direct sideloaders; Obtainium " +
            "and F-Droid already check for updates on their own.",
)

@Composable
fun FaqScreen() {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text(
            text = "Frequently asked questions",
            style = MaterialTheme.typography.headlineSmall,
        )
        faqEntries.forEach { (question, answer) ->
            FaqItem(question = question, answer = answer)
        }
    }
}

@Composable
private fun FaqItem(question: String, answer: String) {
    var expanded by remember { mutableStateOf(false) }

    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable { expanded = !expanded },
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    text = question,
                    style = MaterialTheme.typography.titleMedium,
                    modifier = Modifier.weight(1f),
                )
                Icon(
                    imageVector = if (expanded) Icons.Filled.ExpandLess else Icons.Filled.ExpandMore,
                    contentDescription = if (expanded) "Collapse" else "Expand",
                )
            }
            if (expanded) {
                Text(
                    text = answer,
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.padding(top = 8.dp),
                )
            }
        }
    }
}
