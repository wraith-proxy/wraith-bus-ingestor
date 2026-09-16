package com.thomaspeoples.wraithbus.update

import com.thomaspeoples.wraithbus.BuildConfig
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray

/**
 * Neither F-Droid nor Obtainium need this app's help to notice a new
 * release -- both already watch this repo's Gitea Releases on their own.
 * This exists for the third path: someone who sideloaded the APK directly
 * and has neither installed. It fails silently on any error (no network,
 * a malformed response, an unreachable host) -- a missed update check is
 * not worth interrupting the app over.
 */
object UpdateChecker {
    private val client = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(8, TimeUnit.SECONDS)
        .build()

    private const val RELEASES_URL =
        "https://git.thomaspeoples.com/api/v1/repos/thomaspeoples/wraith-bus-ingestor/releases?limit=20"

    // Matches the android-X.Y.Z-debug tags this app is actually released
    // under -- the same repo also cuts vX.Y.Z tags for the Python package
    // on every merge to main (see .gitea/workflows/release.yml), which
    // this must not be confused with.
    private val TAG_RE = Regex("""^android-(\d+)\.(\d+)\.(\d+)-debug$""")

    data class UpdateInfo(
        val latestVersionName: String,
        val releaseUrl: String,
    )

    private fun parseVersion(name: String): Triple<Int, Int, Int>? =
        TAG_RE.matchEntire(name)?.destructured?.let { (maj, min, patch) ->
            Triple(maj.toInt(), min.toInt(), patch.toInt())
        }

    private fun isNewer(candidate: Triple<Int, Int, Int>, current: Triple<Int, Int, Int>): Boolean {
        if (candidate.first != current.first) return candidate.first > current.first
        if (candidate.second != current.second) return candidate.second > current.second
        return candidate.third > current.third
    }

    /** Null on any failure, and null when already current -- a dialog only
     * ever appears for a real, confirmed newer release. */
    suspend fun checkForUpdate(): UpdateInfo? = withContext(Dispatchers.IO) {
        try {
            val request = Request.Builder().url(RELEASES_URL).get().build()
            val body = client.newCall(request).execute().use { resp ->
                if (!resp.isSuccessful) return@withContext null
                resp.body?.string() ?: return@withContext null
            }
            val releases = JSONArray(body)
            val current = parseVersion("android-${BuildConfig.VERSION_NAME}-debug") ?: return@withContext null

            var best: Pair<Triple<Int, Int, Int>, UpdateInfo>? = null
            for (i in 0 until releases.length()) {
                val release = releases.getJSONObject(i)
                if (release.optBoolean("draft", false)) continue
                val tag = release.optString("tag_name", "")
                val version = parseVersion(tag) ?: continue
                if (!isNewer(version, current)) continue
                if (best == null || isNewer(version, best.first)) {
                    val htmlUrl = release.optString("html_url", "")
                    best = version to UpdateInfo(
                        latestVersionName = "${version.first}.${version.second}.${version.third}",
                        releaseUrl = htmlUrl,
                    )
                }
            }
            best?.second
        } catch (_: Exception) {
            null
        }
    }
}
