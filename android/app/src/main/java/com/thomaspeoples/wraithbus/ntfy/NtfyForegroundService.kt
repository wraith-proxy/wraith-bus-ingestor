package com.thomaspeoples.wraithbus.ntfy

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.thomaspeoples.wraithbus.BuildConfig
import com.thomaspeoples.wraithbus.R
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject

class NtfyForegroundService : Service() {

    companion object {
        const val CHANNEL_ID = "wraith_alerts"
        private const val SERVICE_CHANNEL_ID = "wraith_service"
        private const val FOREGROUND_NOTIFICATION_ID = 1
        private const val BASE_BACKOFF_MS = 2_000L
        private const val MAX_BACKOFF_MS = 60_000L
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val alertNotificationId = AtomicInteger(FOREGROUND_NOTIFICATION_ID + 1)
    private val client = OkHttpClient.Builder()
        .pingInterval(30, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.SECONDS)
        .build()

    private var webSocket: WebSocket? = null
    private var reconnectJob: Job? = null
    private var backoffMs = BASE_BACKOFF_MS

    override fun onCreate() {
        super.onCreate()
        createNotificationChannels()
        startForeground(FOREGROUND_NOTIFICATION_ID, buildForegroundNotification())
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        connect()
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        reconnectJob?.cancel()
        webSocket?.close(1000, "service destroyed")
        webSocket = null
        scope.cancel()
        super.onDestroy()
    }

    private fun createNotificationChannels() {
        val manager = getSystemService(NotificationManager::class.java)

        val serviceChannel = NotificationChannel(
            SERVICE_CHANNEL_ID,
            getString(R.string.ntfy_service_notification_title),
            NotificationManager.IMPORTANCE_LOW,
        ).apply { setSound(null, null) }
        manager.createNotificationChannel(serviceChannel)

        val alertChannel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.notification_channel_alerts),
            NotificationManager.IMPORTANCE_HIGH,
        ).apply { description = getString(R.string.notification_channel_alerts_desc) }
        manager.createNotificationChannel(alertChannel)
    }

    private fun buildForegroundNotification(): Notification =
        NotificationCompat.Builder(this, SERVICE_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.ntfy_service_notification_title))
            .setContentText(getString(R.string.ntfy_service_notification_text))
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setOngoing(true)
            .build()

    private fun connect() {
        if (webSocket != null) return
        reconnectJob?.cancel()
        val request = Request.Builder().url(BuildConfig.NTFY_WS_URL).build()
        webSocket = client.newWebSocket(request, listener)
    }

    private val listener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            backoffMs = BASE_BACKOFF_MS
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            handleMessage(text)
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            this@NtfyForegroundService.webSocket = null
            scheduleReconnect()
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            this@NtfyForegroundService.webSocket = null
            scheduleReconnect()
        }
    }

    private fun scheduleReconnect() {
        reconnectJob?.cancel()
        val delayMs = backoffMs
        backoffMs = (backoffMs * 2).coerceAtMost(MAX_BACKOFF_MS)
        reconnectJob = scope.launch {
            delay(delayMs)
            connect()
        }
    }

    private fun handleMessage(text: String) {
        val json = try {
            JSONObject(text)
        } catch (_: Exception) {
            return
        }

        if (json.optString("event") != "message") return

        val title = json.optString("title").takeIf { it.isNotBlank() } ?: getString(R.string.app_name)
        val message = json.optString("message")

        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title)
            .setContentText(message)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .build()

        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(alertNotificationId.getAndIncrement(), notification)
    }
}
