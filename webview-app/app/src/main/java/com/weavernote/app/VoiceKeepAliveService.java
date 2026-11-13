// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

package com.weavernote.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

import androidx.core.app.NotificationCompat;

/**
 * 语音会话保活前台服务（2026-11-12，Issue #3：手机端退后台语音中断）。
 *
 * 背景：Android 9+ 禁止后台应用访问麦克风；targetSdk 34 起前台服务必须声明
 * 类型，否则「麦克风失效、远端静音」是平台文档明示行为。WebView 里的
 * getUserMedia/AudioContext 随进程后台化被系统掐断——唯一合规路径是语音会话
 * 期间挂一个 microphone|mediaPlayback 前台服务（+ PARTIAL_WAKE_LOCK 防待机
 * CPU 冻结）。
 *
 * 生命周期：前端语音会话建立（用户在页面前台发起，满足 Android 14 的 FGS
 * 启动资格）经桥 setVoiceKeepAlive(true) 启动；挂断/终态/重试耗尽 setVoiceKeepAlive(false)
 * 停止。START_NOT_STICKY：进程被杀时 WebView 会话已同亡，服务不得独自复活
 * 占着通知与唤醒锁。
 */
public class VoiceKeepAliveService extends Service {

    public static final String ACTION_START = "com.weavernote.app.action.VOICE_START";
    public static final String ACTION_STOP = "com.weavernote.app.action.VOICE_STOP";

    private static final String TAG = "WeaverNote";
    private static final String CHANNEL_ID = "voice_session";
    private static final int NOTIFICATION_ID = 42;
    /** 唤醒锁安全上限：桥 stop 丢失时兜底释放，防永久耗电（6 小时）。 */
    private static final long WAKELOCK_SAFETY_MS = 6 * 60 * 60 * 1000L;

    private PowerManager.WakeLock wakeLock;

    @Override
    public void onCreate() {
        super.onCreate();
        createChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            stopSelfGracefully();
            return START_NOT_STICKY;
        }
        try {
            startForegroundWithTypes();
        } catch (Exception e) {
            // 类型资格被拒（如系统限制）→ 降级为无类型前台服务仍好于会话裸奔
            Log.w(TAG, "typed startForeground failed, falling back untyped", e);
            try {
                startForeground(NOTIFICATION_ID, buildNotification());
            } catch (Exception fatal) {
                Log.e(TAG, "startForeground failed outright", fatal);
                stopSelf();
                return START_NOT_STICKY;
            }
        }
        acquireWakeLock();
        return START_NOT_STICKY;
    }

    private void startForegroundWithTypes() {
        Notification notification = buildNotification();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            // Android 11+：麦克风 + 播放双类型（14 强制逐类型 manifest 权限）
            startForeground(NOTIFICATION_ID, notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
                            | ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            // Android 10：microphone 类型尚未引入，mediaPlayback 已可保住播放
            startForeground(NOTIFICATION_ID, notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }
    }

    private Notification buildNotification() {
        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        int piFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            piFlags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent pi = PendingIntent.getActivity(this, 0, open, piFlags);
        return new NotificationCompat.Builder(this, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_splash_logo)
                .setContentTitle(getString(R.string.app_name))
                .setContentText("语音会话进行中")
                .setContentIntent(pi)
                .setOngoing(true)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .build();
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID, "语音会话", NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("语音对话期间保持麦克风与播放通道活跃");
            channel.setShowBadge(false);
            if (nm != null) nm.createNotificationChannel(channel);
        }
    }

    private void acquireWakeLock() {
        if (wakeLock == null) {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm == null) return;
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "weavernote:voice");
            wakeLock.setReferenceCounted(false);
        }
        if (!wakeLock.isHeld()) {
            try {
                wakeLock.acquire(WAKELOCK_SAFETY_MS);
            } catch (Exception e) {
                Log.w(TAG, "wake lock acquire failed", e);
            }
        }
    }

    private void releaseWakeLock() {
        if (wakeLock != null && wakeLock.isHeld()) {
            try { wakeLock.release(); } catch (Exception ignored) { }
        }
    }

    private void stopSelfGracefully() {
        stopForeground(true);
        releaseWakeLock();
        stopSelf();
    }

    @Override
    public void onDestroy() {
        releaseWakeLock();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
