package com.thomaspeoples.wraithbus.ui.update

import android.content.Intent
import android.net.Uri
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import com.thomaspeoples.wraithbus.BuildConfig
import com.thomaspeoples.wraithbus.R
import com.thomaspeoples.wraithbus.update.UpdateChecker

/** Only ever shown for a confirmed newer release (UpdateChecker.checkForUpdate()
 * returns null both on failure and when already current) -- no "up to date"
 * state to render here, just this one case. */
@Composable
fun UpdateAvailableDialog(info: UpdateChecker.UpdateInfo, onDismiss: () -> Unit) {
    val context = LocalContext.current

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(stringResource(R.string.update_available_title)) },
        text = {
            Text(
                stringResource(
                    R.string.update_available_body,
                    info.latestVersionName,
                    BuildConfig.VERSION_NAME,
                )
            )
        },
        confirmButton = {
            TextButton(onClick = {
                context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(info.releaseUrl)))
                onDismiss()
            }) {
                Text(stringResource(R.string.update_available_confirm))
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text(stringResource(R.string.update_available_dismiss))
            }
        },
    )
}
