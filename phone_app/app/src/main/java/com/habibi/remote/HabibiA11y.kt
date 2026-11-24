package com.habibi.remote

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Path
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/** Accessibility service: Home/Back/Recents, tap by text, type, read the screen. */
class HabibiA11y : AccessibilityService() {

    companion object {
        @Volatile var instance: HabibiA11y? = null
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onUnbind(intent: Intent?): Boolean {
        instance = null
        return super.onUnbind(intent)
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}
    override fun onInterrupt() {}

    fun press(button: String): Boolean {
        val action = when (button.lowercase()) {
            "home" -> GLOBAL_ACTION_HOME
            "back" -> GLOBAL_ACTION_BACK
            "recents" -> GLOBAL_ACTION_RECENTS
            "notifications" -> GLOBAL_ACTION_NOTIFICATIONS
            "lock" -> if (Build.VERSION.SDK_INT >= 28) GLOBAL_ACTION_LOCK_SCREEN else return false
            else -> return false
        }
        return performGlobalAction(action)
    }

    fun tapText(text: String): String {
        val root = rootInActiveWindow ?: return "I can't see the screen right now."
        val nodes = root.findAccessibilityNodeInfosByText(text)
        if (nodes.isNullOrEmpty()) return "Nothing on the screen says \"$text\"."
        val node = nodes[0]
        var cur: AccessibilityNodeInfo? = node
        while (cur != null && !cur.isClickable) cur = cur.parent
        if (cur != null && cur.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return "Tapped \"$text\"."
        val r = Rect()
        node.getBoundsInScreen(r)
        val path = Path().apply { moveTo(r.exactCenterX(), r.exactCenterY()) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 60))
            .build()
        return if (dispatchGesture(gesture, null, null)) "Tapped \"$text\"." else "Couldn't tap \"$text\"."
    }

    fun typeText(text: String): String {
        val root = rootInActiveWindow ?: return "I can't see the screen right now."
        val field = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
            ?: return "No text box is selected — tap one first."
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        return if (field.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) "Typed it."
        else "That box doesn't accept typing."
    }

    fun readScreen(): String {
        val root = rootInActiveWindow ?: return ""
        val sb = StringBuilder()
        walk(root, sb, 0)
        return sb.toString().take(3000)
    }

    private fun walk(n: AccessibilityNodeInfo?, sb: StringBuilder, depth: Int) {
        if (n == null || depth > 40 || sb.length > 3000) return
        val t = n.text?.toString()?.trim().orEmpty()
            .ifEmpty { n.contentDescription?.toString()?.trim().orEmpty() }
        if (t.isNotEmpty()) sb.append(t).append('\n')
        for (i in 0 until n.childCount) walk(n.getChild(i), sb, depth + 1)
    }
}
