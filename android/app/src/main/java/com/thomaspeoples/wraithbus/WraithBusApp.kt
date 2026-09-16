package com.thomaspeoples.wraithbus

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import org.maplibre.android.MapLibre

class WraithBusApp : Application() {

    override fun onCreate() {
        super.onCreate()
        MapLibre.getInstance(this)
        createAlertsNotificationChannel()
    }

    private fun createAlertsNotificationChannel() {
        val channel = NotificationChannel(
            "wraith_alerts",
            getString(R.string.notification_channel_alerts),
            NotificationManager.IMPORTANCE_HIGH,
        ).apply {
            description = getString(R.string.notification_channel_alerts_desc)
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }
}
