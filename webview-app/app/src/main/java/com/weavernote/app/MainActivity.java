// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

package com.weavernote.app;

import android.Manifest;
import android.app.Activity;
import android.app.DownloadManager;
import android.content.ClipData;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.util.Base64;
import android.util.Log;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.view.Window;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.SslErrorHandler;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.net.http.SslError;
import android.widget.Button;
import android.widget.ProgressBar;
import android.widget.Toast;

import androidx.activity.result.ActivityResult;
import androidx.activity.result.ActivityResultCallback;
import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;

public class MainActivity extends AppCompatActivity {

    private static final String TAG = "WeaverNote";
    private static final int PERMISSION_REQUEST_CODE = 1001;

    private WebView webView;
    private ProgressBar progressBar;
    private View splashOverlay;
    /** 离线重试遮罩（Issue #2）：主文档加载失败时可见，成功后隐藏。 */
    private View offlineOverlay;
    /** 自动 reload 去抖：网络抖动（onAvailable 连发）不得把 WebView 打进重载循环。 */
    private long lastReloadAt = 0L;
    private ConnectivityManager.NetworkCallback networkCallback;
    private WindowInsetsControllerCompat insetsController;
    /** 前端已下发过一次真实系统栏颜色（新桥 onThemeColors）→ 旧布尔桥的硬编码色不再覆盖。 */
    private volatile boolean webColorsApplied = false;
    /** 最近一次 WindowInsets 系统栏 inset（px），页面加载完成后重注入。 */
    private int lastSafeTop = 0;
    private int lastSafeBottom = 0;
    private int lastImeBottom = -1;

    /** 把安全区写成 CSS 变量注入当前文档（页面切换/重载后由 onPageFinished 重放）。 */
    private void injectSafeAreas() {
        if (webView == null) return;
        String js = "(function(){var s=document.documentElement.style;"
                + "s.setProperty('--wt-safe-top','" + lastSafeTop + "px');"
                + "s.setProperty('--wt-safe-bottom','" + lastSafeBottom + "px');})()";
        webView.evaluateJavascript(js, null);
    }

    private ValueCallback<Uri[]> fileUploadCallback;
    private ActivityResultLauncher<Intent> fileChooserLauncher;

    // ========================================
    // Named inner class for JS Bridge
    // ========================================
    public class WebAppInterface {
        private final Context context;

        WebAppInterface(Context context) {
            this.context = context;
        }

        @JavascriptInterface
        public void onThemeChanged(boolean isDark) {
            // 兼容路径（旧前端/注入 observer）：透明边到边后只控制图标深浅。
            // 前端已下发过表面色时不再覆盖其亮度判定。
            if (webColorsApplied) return;
            runOnUiThread(() -> {
                if (webColorsApplied) return;
                insetsController.setAppearanceLightStatusBars(!isDark);
                insetsController.setAppearanceLightNavigationBars(!isDark);
            });
        }

        /**
         * 新桥（wave-2 语义）：前端下发屏幕顶边/底边实际绘制色。系统栏透明，
         * 颜色本身不再设置到窗口，仅按亮度决定各栏图标深浅（luminance>0.5 → 深色图标）。
         * 非法颜色串忽略并保持现状，不崩溃。
         */
        @JavascriptInterface
        public void onThemeColors(String statusHex, String navHex) {
            runOnUiThread(() -> {
                try {
                    int status = Color.parseColor(statusHex);
                    int nav = Color.parseColor(navHex);
                    webColorsApplied = true;
                    insetsController.setAppearanceLightStatusBars(Color.luminance(status) > 0.5f);
                    insetsController.setAppearanceLightNavigationBars(Color.luminance(nav) > 0.5f);
                } catch (IllegalArgumentException | NullPointerException e) {
                    Log.w(TAG, "onThemeColors ignored (invalid color): "
                            + (statusHex == null ? "null" : statusHex.length()) + "/"
                            + (navHex == null ? "null" : navHex.length()));
                }
            });
        }

        @JavascriptInterface
        public boolean saveFile(String base64Data, String filename, String mimeType) {
            Log.d(TAG, "saveFile called: " + filename + " mimeType=" + mimeType + " dataLen=" + (base64Data != null ? base64Data.length() : 0));
            try {
                byte[] data = Base64.decode(base64Data, Base64.DEFAULT);
                Log.d(TAG, "Decoded bytes: " + data.length);

                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    // Android 10+ : MediaStore Downloads
                    ContentValues values = new ContentValues();
                    values.put(MediaStore.Downloads.DISPLAY_NAME, filename);
                    values.put(MediaStore.Downloads.MIME_TYPE, mimeType);
                    values.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
                    values.put(MediaStore.Downloads.IS_PENDING, 1);

                    Uri uri = context.getContentResolver().insert(
                            MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
                    if (uri == null) {
                        Log.e(TAG, "MediaStore insert returned null URI");
                        showSaveResult(false, filename);
                        return false;
                    }

                    try (OutputStream os = context.getContentResolver().openOutputStream(uri)) {
                        if (os != null) {
                            os.write(data);
                            os.flush();
                        }
                    }

                    // Mark as complete
                    ContentValues update = new ContentValues();
                    update.put(MediaStore.Downloads.IS_PENDING, 0);
                    context.getContentResolver().update(uri, update, null, null);

                    Log.d(TAG, "File saved via MediaStore: " + uri);
                } else {
                    // Android 9 and below
                    File dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS);
                    if (!dir.exists()) dir.mkdirs();
                    File file = new File(dir, filename);
                    try (FileOutputStream fos = new FileOutputStream(file)) {
                        fos.write(data);
                        fos.flush();
                    }
                    Log.d(TAG, "File saved directly: " + file.getAbsolutePath());
                }

                showSaveResult(true, filename);
                return true;
            } catch (Exception e) {
                Log.e(TAG, "saveFile failed", e);
                showSaveResult(false, filename);
                return false;
            }
        }

        @JavascriptInterface
        public boolean downloadFile(String url, String filename) {
            Log.d(TAG, "downloadFile called: url=" + url + " filename=" + filename);

            if (url == null || url.isEmpty()) return false;

            try {
                Uri uri = Uri.parse(url);
                String scheme = uri.getScheme();
                if (scheme == null) scheme = "";

                // Relative URLs (e.g. "/api/files/download?path=...&token=..." passed
                // by the web frontend) parse to an empty scheme — resolve them
                // against the current page before DownloadManager sees them.
                if (scheme.isEmpty()) {
                    String baseUrl = webView.getUrl();
                    if (baseUrl != null) {
                        Uri base = Uri.parse(baseUrl);
                        int qIdx = url.indexOf('?');
                        String path = qIdx >= 0 ? url.substring(0, qIdx) : url;
                        String query = qIdx >= 0 ? url.substring(qIdx + 1) : null;
                        Uri.Builder builder = base.buildUpon().encodedPath(path);
                        if (query != null) {
                            builder.encodedQuery(query);
                        } else {
                            // 不带 query 的相对 URL 不得继承当前页面的 query/fragment
                            builder.clearQuery().fragment(null);
                        }
                        uri = builder.build();
                        scheme = uri.getScheme() != null ? uri.getScheme() : "";
                        Log.d(TAG, "Resolved relative URL -> " + uri);
                    }
                }

                if ("http".equals(scheme) || "https".equals(scheme)) {
                    DownloadManager.Request request = new DownloadManager.Request(uri);
                    request.setTitle(filename);
                    request.setNotificationVisibility(
                            DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                    request.setDestinationInExternalPublicDir(
                            Environment.DIRECTORY_DOWNLOADS, filename);
                    String cookies = CookieManager.getInstance().getCookie(uri.toString());
                    if (cookies != null) {
                        request.addRequestHeader("cookie", cookies);
                    }
                    request.addRequestHeader("User-Agent",
                            webView.getSettings().getUserAgentString());

                    DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
                    dm.enqueue(request);

                    runOnUiThread(() -> Toast.makeText(context,
                            "正在下载: " + filename, Toast.LENGTH_SHORT).show());
                    return true;
                }

                if ("blob".equals(scheme)) {
                    Log.d(TAG, "Blob URL not supported by DownloadManager, use saveFile for base64");
                    return false;
                }

                Log.d(TAG, "Unsupported URL scheme: " + scheme);
                return false;
            } catch (Exception e) {
                Log.e(TAG, "downloadFile failed", e);
                return false;
            }
        }

        @JavascriptInterface
        public boolean downloadFileWithProgress(String url, String filename, String id) {
            Log.d(TAG, "downloadFileWithProgress url=" + url + " name=" + filename + " id=" + id);
            return downloadFile(url, filename);
        }

        @JavascriptInterface
        public boolean isDownloadSupported() {
            return true;
        }

        @JavascriptInterface
        public void exitApp() {
            runOnUiThread(() -> finishAffinity());
        }

        /**
         * 语音会话保活桥（2026-11-12，Issue #3）：前端在语音会话建立/终态时调用。
         * active=true → 启动 microphone|mediaPlayback 前台服务 + WakeLock（后台
         * 续命麦克风与播放）；false → 释放。O+ 一律走 startForegroundService：
         * STOP 路径若用 startService 会在后台被 BackgroundServiceStartNotAllowed
         * 拒掉，前台服务化后「启动即停」是合法路径。
         */
        @JavascriptInterface
        public void setVoiceKeepAlive(boolean active) {
            try {
                Intent i = new Intent(context, VoiceKeepAliveService.class);
                if (!active) {
                    // STOP 直接 stopService()：对已运行服务后台调用合法；
                    // 若走 startForegroundService(ACTION_STOP)，退后台终态时平台可能
                    // 拒绝后台 FGS 启动（catch 吞掉）→ 通知+WakeLock 挂到 6h 兜底。
                    context.stopService(i);
                    return;
                }
                i.setAction(VoiceKeepAliveService.ACTION_START);
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(i);
                } else {
                    context.startService(i);
                }
            } catch (Exception e) {
                Log.w(TAG, "setVoiceKeepAlive(" + active + ") failed", e);
            }
        }

        /** Open an http(s) URL in the system browser instead of navigating the SPA WebView. */
        @JavascriptInterface
        public boolean openUrl(String url) {
            if (url == null || url.isEmpty()) return false;
            try {
                Uri uri = Uri.parse(url);
                String scheme = uri.getScheme();
                if (scheme == null) return false;
                scheme = scheme.toLowerCase();
                if (!"http".equals(scheme) && !"https".equals(scheme)) return false;
                Intent intent = new Intent(Intent.ACTION_VIEW, uri);
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                context.startActivity(intent);
                return true;
            } catch (Exception e) {
                Log.e(TAG, "openUrl failed: " + url, e);
                return false;
            }
        }

        private void showSaveResult(boolean success, String filename) {
            runOnUiThread(() -> {
                if (success) {
                    Toast.makeText(context, "已保存到下载目录: " + filename, Toast.LENGTH_LONG).show();
                } else {
                    Toast.makeText(context, "保存失败: " + filename, Toast.LENGTH_SHORT).show();
                }
            });
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // wave-2 透明边到边：系统栏透明，内容铺满；安全区经 --wt-safe-* 注入由页面消费；
        // 图标深浅由前端下发的表面亮度决定（onThemeColors/onThemeChanged）。
        Window window = getWindow();
        WindowCompat.setDecorFitsSystemWindows(window, false);
        // AppCompat 的 ActionBarOverlayLayout 会自行消费 inset（真机实证：仅
        // setDecorFitsSystemWindows(false) 时内容仍被系统带压住一条黑边），
        // 需显式关掉 decor 的 fitsSystemWindows 并允许绘制到刘海/挖孔带。
        window.getDecorView().setFitsSystemWindows(false);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            window.getAttributes().layoutInDisplayCutoutMode =
                    WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
        }
        window.setStatusBarColor(Color.TRANSPARENT);
        window.setNavigationBarColor(Color.TRANSPARENT);
        insetsController = WindowCompat.getInsetsController(window, window.getDecorView());
        insetsController.setAppearanceLightStatusBars(true);
        insetsController.setAppearanceLightNavigationBars(true);

        fileChooserLauncher = registerForActivityResult(
            new ActivityResultContracts.StartActivityForResult(),
            new ActivityResultCallback<ActivityResult>() {
                @Override
                public void onActivityResult(ActivityResult result) {
                    Uri[] results = null;
                    if (result.getResultCode() == Activity.RESULT_OK && result.getData() != null) {
                        results = extractUris(result.getData());
                    }
                    if (fileUploadCallback != null) {
                        fileUploadCallback.onReceiveValue(results);
                        fileUploadCallback = null;
                    }
                }
            }
        );

        setContentView(R.layout.activity_main);

        webView = findViewById(R.id.webview);
        progressBar = findViewById(R.id.progressbar);
        splashOverlay = findViewById(R.id.splash_overlay);

        // 离线重试遮罩（Issue #2）：加载失败→可见；手动重试立即 reload。
        offlineOverlay = findViewById(R.id.offline_overlay);
        Button retryButton = findViewById(R.id.offline_retry_button);
        if (retryButton != null) {
            retryButton.setOnClickListener(v -> reloadNow());
        }

        // 安全区注入：Android WebView 内 env(safe-area-inset-top) 可能为 0（业界同坑，
        // 壳侧通行做法=原生读 WindowInsets 注入 CSS 变量）。
        // ⚠ WindowInsets 是物理像素，CSS 变量必须是 CSS px（=dp）——除以 density，
        // 否则安全区被放大 density 倍（真机实证：36dp 状态栏变 75dp 空白）。
        View contentRoot = findViewById(android.R.id.content);
        final float density = getResources().getDisplayMetrics().density;
        ViewCompat.setOnApplyWindowInsetsListener(contentRoot, (v, insets) -> {
            Insets bars = insets.getInsets(
                    WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout());
            int cssTop = Math.round(bars.top / density);
            int cssBottom = Math.round(bars.bottom / density);
            if (cssTop != lastSafeTop || cssBottom != lastSafeBottom) {
                lastSafeTop = cssTop;
                lastSafeBottom = cssBottom;
                injectSafeAreas();
            }
            // 键盘避让（2026-11-10 真机回归修复）：边到边（decorFitsSystemWindows=false）
            // 下 adjustResize 不再收缩 WebView——IME 改以 WindowInsets 下发，必须自行消费，
            // 否则键盘静默覆盖页面（WebView <M139 时 innerHeight/vv.height 均无变化，JS 无信号）。
            // 用 bottomMargin 收缩 WebView = 恢复「布局视口压缩」：innerHeight/100dvh 随键盘
            // 缩小，前端外壳自然顶起，且 vv 同步缩小 → 前端 --keyboard-inset=0 不双重顶起。
            Insets ime = insets.getInsets(WindowInsetsCompat.Type.ime());
            if (ime.bottom != lastImeBottom) {
                lastImeBottom = ime.bottom;
                ViewGroup.MarginLayoutParams lp =
                        (ViewGroup.MarginLayoutParams) webView.getLayoutParams();
                if (lp.bottomMargin != ime.bottom) {
                    lp.bottomMargin = ime.bottom;
                    webView.setLayoutParams(lp);
                }
            }
            return insets;
        });
        contentRoot.requestApplyInsets();

        setupWebView();
        requestPermissions();
        registerNetworkMonitor();

        webView.loadUrl(BuildConfig.SERVER_URL);
    }

    /** 手动/自动重试：去抖 2s，防网络抖动连发把 WebView 打进重载循环。 */
    private void reloadNow() {
        long now = System.currentTimeMillis();
        if (now - lastReloadAt < 2000L) return;
        lastReloadAt = now;
        if (offlineOverlay != null) offlineOverlay.setVisibility(View.GONE);
        if (webView != null) webView.reload();
    }

    /** 网络恢复监听（Issue #2）：仅在离线遮罩可见（=上次加载失败）时自动 reload。 */
    private void registerNetworkMonitor() {
        try {
            ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) return;
            networkCallback = new ConnectivityManager.NetworkCallback() {
                @Override
                public void onAvailable(Network network) {
                    runOnUiThread(() -> {
                        if (offlineOverlay != null && offlineOverlay.getVisibility() == View.VISIBLE) {
                            reloadNow();
                        }
                    });
                }
            };
            cm.registerDefaultNetworkCallback(networkCallback);
        } catch (Exception e) {
            Log.w(TAG, "network monitor unavailable", e);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        // 与 WebView 默认行为对齐（我们从不 onPause 页面，这里只做防御性恢复；
        // 语音/流媒体的后台存活由 VoiceKeepAliveService 负责，不靠暂停语义）。
        if (webView != null) webView.onResume();
    }

    @Override
    protected void onDestroy() {
        if (networkCallback != null) {
            try {
                ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
                if (cm != null) cm.unregisterNetworkCallback(networkCallback);
            } catch (Exception ignored) { }
            networkCallback = null;
        }
        // Activity 消亡=WebView 会话同亡：兜底停掉保活服务，防通知/唤醒锁残留
        try {
            stopService(new Intent(this, VoiceKeepAliveService.class));
        } catch (Exception ignored) { }
        super.onDestroy();
    }

    private void setupWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);
        settings.setDatabaseEnabled(true);

        // 保持页面在 WebView 内打开，并信任自签名证书
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                handler.proceed();
            }

            /**
             * 主文档加载失败（服务器不可达/断网/DNS）→ 显示离线遮罩（Issue #2）。
             * 旧实现没有任何恢复路径：系统错误页盖在 WebView 上，用户只能杀进程
             * 重启。现在网络恢复由 NetworkCallback 自动 reload，也可手动点重试。
             */
            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request != null && request.isForMainFrame()) {
                    Log.w(TAG, "Main frame load failed: " + error.getErrorCode()
                            + " " + error.getDescription() + " url=" + request.getUrl());
                    runOnUiThread(() -> {
                        if (offlineOverlay != null) offlineOverlay.setVisibility(View.VISIBLE);
                    });
                }
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                // 文档加载成功 → 收起离线遮罩
                if (offlineOverlay != null && offlineOverlay.getVisibility() == View.VISIBLE) {
                    offlineOverlay.setVisibility(View.GONE);
                }
                // 新文档重放安全区注入（inset 值进程内缓存）
                injectSafeAreas();
                // Hide splash with fade-out animation
                if (splashOverlay != null && splashOverlay.getVisibility() == View.VISIBLE) {
                    splashOverlay.animate()
                        .alpha(0f)
                        .setDuration(400)
                        .withEndAction(() -> {
                            splashOverlay.setVisibility(View.GONE);
                        })
                        .start();
                }

                view.evaluateJavascript(
                    "(function() {" +
                    "  function notifyTheme() {" +
                    "    var isDark = document.documentElement.getAttribute('data-theme') === 'dark';" +
                    "    WeaverNoteApp.onThemeChanged(isDark);" +
                    "  }" +
                    "  notifyTheme();" +
                    "  new MutationObserver(function() { notifyTheme(); })" +
                    "    .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });" +
                    "})()", null);
            }
        });

        // JS Bridge (named class — reliable across WebView versions)
        webView.addJavascriptInterface(new WebAppInterface(this), "WeaverNoteApp");

        // DownloadListener — catches HTTP download URLs
        webView.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent,
                    String contentDisposition, String mimeType, long contentLength) {
                Log.d(TAG, "DownloadListener: url=" + url + " mime=" + mimeType);
                try {
                    DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
                    request.setMimeType(mimeType);
                    String cookies = CookieManager.getInstance().getCookie(url);
                    if (cookies != null) {
                        request.addRequestHeader("cookie", cookies);
                    }
                    request.addRequestHeader("User-Agent", userAgent);
                    String filename = URLUtil.guessFileName(url, contentDisposition, mimeType);
                    request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, filename);
                    request.setNotificationVisibility(
                            DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                    request.setTitle(filename);

                    DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
                    dm.enqueue(request);

                    Toast.makeText(MainActivity.this, "正在下载: " + filename, Toast.LENGTH_SHORT).show();
                } catch (Exception e) {
                    Log.e(TAG, "DownloadManager failed", e);
                    Toast.makeText(MainActivity.this, "下载失败", Toast.LENGTH_SHORT).show();
                }
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                if (newProgress < 100) {
                    progressBar.setVisibility(View.VISIBLE);
                    progressBar.setProgress(newProgress);
                } else {
                    progressBar.setVisibility(View.GONE);
                }
            }

            @Override
            public void onPermissionRequest(PermissionRequest request) {
                runOnUiThread(() -> request.grant(request.getResources()));
            }

            @Override
            public boolean onShowFileChooser(WebView webView,
                    ValueCallback<Uri[]> filePathCallback,
                    FileChooserParams fileChooserParams) {
                if (fileUploadCallback != null) {
                    fileUploadCallback.onReceiveValue(null);
                }
                fileUploadCallback = filePathCallback;

                Intent intent = null;
                try {
                    intent = fileChooserParams.createIntent();
                } catch (Exception e) {
                    Log.w(TAG, "fileChooserParams.createIntent() failed, using fallback", e);
                }

                if (intent == null) {
                    intent = new Intent(Intent.ACTION_GET_CONTENT);
                    intent.addCategory(Intent.CATEGORY_OPENABLE);
                    intent.setType("*/*");
                    String[] acceptTypes = fileChooserParams.getAcceptTypes();
                    if (acceptTypes != null && acceptTypes.length > 0 && !"".equals(acceptTypes[0])) {
                        intent.putExtra(Intent.EXTRA_MIME_TYPES, acceptTypes);
                    }
                }

                try {
                    fileChooserLauncher.launch(intent);
                } catch (Exception e) {
                    Log.e(TAG, "File chooser launch failed", e);
                    fileUploadCallback = null;
                    return false;
                }
                return true;
            }
        });
    }

    private Uri[] extractUris(Intent data) {
        List<Uri> uris = new ArrayList<>();

        Uri single = data.getData();
        if (single != null) {
            uris.add(single);
        }

        ClipData clipData = data.getClipData();
        if (clipData != null) {
            for (int i = 0; i < clipData.getItemCount(); i++) {
                Uri clipUri = clipData.getItemAt(i).getUri();
                if (clipUri != null && !uris.contains(clipUri)) {
                    uris.add(clipUri);
                }
            }
        }

        if (uris.isEmpty()) return null;
        return uris.toArray(new Uri[0]);
    }

    private void requestPermissions() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            List<String> needed = new ArrayList<>();
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                needed.add(Manifest.permission.RECORD_AUDIO);
            }
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q
                    && checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
                needed.add(Manifest.permission.WRITE_EXTERNAL_STORAGE);
            }
            // Android 13+ media permissions
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                if (checkSelfPermission(Manifest.permission.READ_MEDIA_IMAGES) != PackageManager.PERMISSION_GRANTED) {
                    needed.add(Manifest.permission.READ_MEDIA_IMAGES);
                }
                if (checkSelfPermission(Manifest.permission.READ_MEDIA_VIDEO) != PackageManager.PERMISSION_GRANTED) {
                    needed.add(Manifest.permission.READ_MEDIA_VIDEO);
                }
                if (checkSelfPermission(Manifest.permission.READ_MEDIA_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                    needed.add(Manifest.permission.READ_MEDIA_AUDIO);
                }
                // 语音保活前台服务的通知（被拒仅影响通知可见性，不影响服务运行）
                if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                    needed.add(Manifest.permission.POST_NOTIFICATIONS);
                }
            }
            if (!needed.isEmpty()) {
                requestPermissions(needed.toArray(new String[0]), PERMISSION_REQUEST_CODE);
            }
        }
    }

    @Override
    public void onBackPressed() {
        webView.evaluateJavascript("window.handleAndroidBack && window.handleAndroidBack()", result -> {
            if ("true".equals(result)) {
                return;
            }
            // The current document may be an external page the SPA navigated to
            // (e.g. a link inside a citation card). Prefer WebView history so the
            // user returns to the SPA instead of exiting the app.
            if (webView.canGoBack()) {
                webView.goBack();
            } else {
                runOnUiThread(() -> MainActivity.super.onBackPressed());
            }
        });
    }
}
