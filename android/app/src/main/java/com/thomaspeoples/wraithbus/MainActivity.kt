package com.thomaspeoples.wraithbus

import android.Manifest
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import com.thomaspeoples.wraithbus.ntfy.NtfyForegroundService
import com.thomaspeoples.wraithbus.ui.nav.AppNav
import com.thomaspeoples.wraithbus.ui.theme.WraithBusTheme

class MainActivity : ComponentActivity() {

    private val requestNotificationPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        requestNotificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)

        // A build with no NTFY_TOPIC baked in (local.properties absent --
        // exactly what happens when F-Droid's build server, or anyone else
        // building from source, checks this repo out fresh) has nothing to
        // subscribe to. Starting the service anyway would hold a
        // persistent foreground notification open for a socket that can
        // never connect to anything real.
        if (BuildConfig.NTFY_TOPIC.isNotBlank()) {
            ContextCompat.startForegroundService(this, Intent(this, NtfyForegroundService::class.java))
        }

        setContent {
            WraithBusTheme {
                AppNav()
            }
        }
    }
}
