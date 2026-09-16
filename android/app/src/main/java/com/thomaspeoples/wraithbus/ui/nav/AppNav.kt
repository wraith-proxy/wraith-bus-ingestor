package com.thomaspeoples.wraithbus.ui.nav

import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Dashboard
import androidx.compose.material.icons.filled.LocationOn
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.thomaspeoples.wraithbus.R
import com.thomaspeoples.wraithbus.ui.dashboard.DashboardScreen
import com.thomaspeoples.wraithbus.ui.tracking.TrackingScreen

private const val ROUTE_TRACKING = "tracking"
private const val ROUTE_DASHBOARD = "dashboard"

@Composable
fun AppNav() {
    val navController = rememberNavController()

    Scaffold(
        bottomBar = { AppBottomBar(navController) },
    ) { innerPadding ->
        NavHost(
            navController = navController,
            startDestination = ROUTE_TRACKING,
            modifier = Modifier.padding(innerPadding),
        ) {
            composable(ROUTE_TRACKING) { TrackingScreen() }
            composable(ROUTE_DASHBOARD) { DashboardScreen() }
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
