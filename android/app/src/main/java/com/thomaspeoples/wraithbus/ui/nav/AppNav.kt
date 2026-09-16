package com.thomaspeoples.wraithbus.ui.nav

import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Dashboard
import androidx.compose.material.icons.filled.LocationOn
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.thomaspeoples.wraithbus.R
import com.thomaspeoples.wraithbus.ui.about.AboutScreen
import com.thomaspeoples.wraithbus.ui.dashboard.DashboardScreen
import com.thomaspeoples.wraithbus.ui.faq.FaqScreen
import com.thomaspeoples.wraithbus.ui.tracking.TrackingScreen
import com.thomaspeoples.wraithbus.ui.update.UpdateAvailableDialog
import com.thomaspeoples.wraithbus.update.UpdateChecker

private const val ROUTE_TRACKING = "tracking"
private const val ROUTE_DASHBOARD = "dashboard"
private const val ROUTE_ABOUT = "about"
private const val ROUTE_FAQ = "faq"

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppNav() {
    val navController = rememberNavController()

    var menuExpanded by remember { mutableStateOf(false) }
    var updateInfo by remember { mutableStateOf<UpdateChecker.UpdateInfo?>(null) }

    // Neither F-Droid nor Obtainium need this app's help to notice a new
    // release -- see UpdateChecker's own docstring. One check per cold
    // start is enough; it fails silently and never blocks the UI.
    LaunchedEffect(Unit) {
        updateInfo = UpdateChecker.checkForUpdate()
    }

    updateInfo?.let { info ->
        UpdateAvailableDialog(info = info, onDismiss = { updateInfo = null })
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(stringResource(R.string.app_name)) },
                actions = {
                    IconButton(onClick = { menuExpanded = true }) {
                        Icon(Icons.Filled.MoreVert, contentDescription = stringResource(R.string.menu_more))
                    }
                    DropdownMenu(expanded = menuExpanded, onDismissRequest = { menuExpanded = false }) {
                        DropdownMenuItem(
                            text = { Text(stringResource(R.string.menu_about)) },
                            onClick = {
                                menuExpanded = false
                                navController.navigate(ROUTE_ABOUT)
                            },
                        )
                        DropdownMenuItem(
                            text = { Text(stringResource(R.string.menu_faq)) },
                            onClick = {
                                menuExpanded = false
                                navController.navigate(ROUTE_FAQ)
                            },
                        )
                    }
                },
            )
        },
        bottomBar = { AppBottomBar(navController) },
    ) { innerPadding ->
        NavHost(
            navController = navController,
            startDestination = ROUTE_TRACKING,
            modifier = Modifier.padding(innerPadding),
        ) {
            composable(ROUTE_TRACKING) { TrackingScreen() }
            composable(ROUTE_DASHBOARD) { DashboardScreen() }
            composable(ROUTE_ABOUT) { AboutScreen() }
            composable(ROUTE_FAQ) { FaqScreen() }
        }
    }
}

@Composable
private fun AppBottomBar(navController: NavHostController) {
    val navBackStackEntry by navController.currentBackStackEntryAsState()
    val currentRoute = navBackStackEntry?.destination?.route

    NavigationBar {
        NavigationBarItem(
            selected = currentRoute == ROUTE_TRACKING,
            onClick = { navigateToTab(navController, ROUTE_TRACKING) },
            icon = { Icon(Icons.Filled.LocationOn, contentDescription = null) },
            label = { Text(stringResource(R.string.nav_tracking)) },
        )
        NavigationBarItem(
            selected = currentRoute == ROUTE_DASHBOARD,
            onClick = { navigateToTab(navController, ROUTE_DASHBOARD) },
            icon = { Icon(Icons.Filled.Dashboard, contentDescription = null) },
            label = { Text(stringResource(R.string.nav_dashboard)) },
        )
    }
}

private fun navigateToTab(navController: NavHostController, route: String) {
    navController.navigate(route) {
        popUpTo(navController.graph.findStartDestination().id) {
            saveState = true
        }
        launchSingleTop = true
        restoreState = true
    }
}
