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
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.util.Base64;
import android.util.Log;
import android.view.View;
import android.view.Window;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.SslErrorHandler;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.net.http.SslError;
import android.widget.ProgressBar;
import android.widget.Toast;

import androidx.activity.result.ActivityResult;
import androidx.activity.result.ActivityResultCallback;
import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.view.WindowCompat;
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
    private WindowInsetsControllerCompat insetsController;

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
            runOnUiThread(() -> {
                Window w = getWindow();
                if (isDark) {
                    w.setStatusBarColor(0xFF202020);
                    w.setNavigationBarColor(0xFF191919);
                    insetsController.setAppearanceLightStatusBars(false);
                    insetsController.setAppearanceLightNavigationBars(false);
                } else {
                    w.setStatusBarColor(0xFFFFFFFF);
                    w.setNavigationBarColor(0xFFF4F6F9);
                    insetsController.setAppearanceLightStatusBars(true);
                    insetsController.setAppearanceLightNavigationBars(true);
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

        // Edge-to-edge: 系统栏颜色匹配 web 主题
        WindowCompat.setDecorFitsSystemWindows(getWindow(), false);
        Window window = getWindow();
        window.setStatusBarColor(0xFFFFFFFF);
        window.setNavigationBarColor(0xFFF4F6F9);
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

        setupWebView();
        requestPermissions();

        webView.loadUrl(BuildConfig.SERVER_URL);
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

            @Override
            public void onPageFinished(WebView view, String url) {
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
            }
            if (!needed.isEmpty()) {
                requestPermissions(needed.toArray(new String[0]), PERMISSION_REQUEST_CODE);
            }
        }
    }

    @Override
    public void onBackPressed() {
        webView.evaluateJavascript("window.handleAndroidBack && window.handleAndroidBack()", result -> {
            if (!"true".equals(result)) {
                runOnUiThread(() -> MainActivity.super.onBackPressed());
            }
        });
    }
}
