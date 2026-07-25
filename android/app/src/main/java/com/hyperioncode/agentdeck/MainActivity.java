package com.hyperioncode.agentdeck;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity implements AgentDeckView.Listener {
    private static final int AUDIO_PERMISSION_REQUEST = 41;
    private static final long POLL_INTERVAL_MS = 2_000;

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final ExecutorService networkExecutor = Executors.newSingleThreadExecutor();
    private AgentDeckView deckView;
    private SpeechRecognizer speechRecognizer;
    private Intent speechIntent;
    private boolean listening;
    private boolean polling;
    private int voiceFeedbackGeneration;
    private volatile String activeBaseUrl;

    private final Runnable pollTask = new Runnable() {
        @Override
        public void run() {
            if (!polling) {
                return;
            }
            networkExecutor.execute(() -> {
                try {
                    JSONObject state = request("/api/v1/state", "GET", null);
                    List<TaskModel> tasks = new ArrayList<>();
                    JSONArray items = state.getJSONArray("threads");
                    for (int index = 0; index < items.length(); index++) {
                        tasks.add(TaskModel.from(items.getJSONObject(index)));
                    }
                    String ledMode = state.optString("ledMode", "idle");
                    String selected = state.optString("selectedThreadId", null);
                    UsageModel usage = UsageModel.from(
                            state.optJSONObject("usage")
                    );
                    mainHandler.post(() -> {
                        deckView.setState(tasks, ledMode, selected, usage);
                        deckView.setConnection(true, connectionLabel());
                    });
                } catch (Exception error) {
                    mainHandler.post(() ->
                            deckView.setConnection(false, "PC UNREACHABLE"));
                } finally {
                    mainHandler.postDelayed(this, POLL_INTERVAL_MS);
                }
            });
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().setStatusBarColor(0xFF05070A);
        getWindow().setNavigationBarColor(0xFF05070A);

        deckView = new AgentDeckView(this);
        deckView.setListener(this);
        setContentView(deckView);
        configureSpeechRecognizer();

        if ((BuildConfig.AGENT_DECK_URL.isEmpty()
                && BuildConfig.AGENT_DECK_LAN_URL.isEmpty())
                || BuildConfig.AGENT_DECK_TOKEN.isEmpty()) {
            deckView.setConnection(false, "APP NOT CONFIGURED");
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        polling = true;
        mainHandler.removeCallbacks(pollTask);
        mainHandler.post(pollTask);
    }

    @Override
    protected void onPause() {
        polling = false;
        mainHandler.removeCallbacks(pollTask);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        if (speechRecognizer != null) {
            speechRecognizer.destroy();
        }
        networkExecutor.shutdownNow();
        super.onDestroy();
    }

    private void configureSpeechRecognizer() {
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            deckView.setVoiceAvailable(false);
            return;
        }
        speechRecognizer = SpeechRecognizer.createSpeechRecognizer(this);
        speechIntent = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        speechIntent.putExtra(
                RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM
        );
        speechIntent.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
        speechIntent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault());
        speechRecognizer.setRecognitionListener(new RecognitionListener() {
            @Override public void onReadyForSpeech(Bundle params) {
                listening = true;
                deckView.setListening(true, "LISTENING");
            }

            @Override public void onBeginningOfSpeech() {
                deckView.setListening(true, "HEARING YOU");
            }

            @Override public void onRmsChanged(float rmsdB) {
                deckView.setVoiceLevel(Math.max(0f, Math.min(1f, rmsdB / 12f)));
            }

            @Override public void onBufferReceived(byte[] buffer) {}
            @Override public void onEndOfSpeech() {
                deckView.setListening(true, "TRANSCRIBING");
            }

            @Override public void onError(int error) {
                listening = false;
                if (error == SpeechRecognizer.ERROR_NO_MATCH
                        || error == SpeechRecognizer.ERROR_SPEECH_TIMEOUT) {
                    deckView.setListening(false, "NO SPEECH HEARD");
                } else {
                    deckView.setListening(false, "VOICE ERROR");
                    toast("Voice recognition stopped");
                }
            }

            @Override public void onResults(Bundle results) {
                listening = false;
                ArrayList<String> matches = results.getStringArrayList(
                        SpeechRecognizer.RESULTS_RECOGNITION
                );
                if (matches != null && !matches.isEmpty()) {
                    String transcript = matches.get(0).trim();
                    if (!transcript.isEmpty()) {
                        deckView.setListening(false, "SENDING");
                        deckView.setVoiceCaption(transcript);
                        sendVoiceMessage(transcript);
                        return;
                    }
                }
                deckView.setListening(false, "NO SPEECH HEARD");
            }

            @Override public void onPartialResults(Bundle partialResults) {
                ArrayList<String> matches = partialResults.getStringArrayList(
                        SpeechRecognizer.RESULTS_RECOGNITION
                );
                if (matches != null && !matches.isEmpty()) {
                    deckView.setVoiceCaption(matches.get(0));
                }
            }

            @Override public void onEvent(int eventType, Bundle params) {}
        });
    }

    @Override
    public void onTaskPressed(TaskModel task) {
        deckView.selectThread(task.id);
        post("/api/v1/threads/" + task.id + "/open", new JSONObject(), "Task opened on PC");
    }

    @Override
    public void onNewPressed() {
        post("/api/v1/actions/new", new JSONObject(), "New Codex task opened");
    }

    @Override
    public void onMicPressed() {
        if (speechRecognizer == null) {
            toast("Speech recognition is unavailable");
            return;
        }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(
                    new String[]{Manifest.permission.RECORD_AUDIO},
                    AUDIO_PERMISSION_REQUEST
            );
            return;
        }
        if (listening) {
            deckView.setListening(true, "TRANSCRIBING");
            speechRecognizer.stopListening();
        } else {
            voiceFeedbackGeneration++;
            deckView.setVoiceCaption("");
            listening = true;
            deckView.setListening(true, "STARTING");
            speechRecognizer.startListening(speechIntent);
        }
    }

    @Override
    public void onArchivePressed() {
        String selected = deckView.getSelectedThreadId();
        if (selected == null) {
            toast("Select a task first");
            return;
        }
        post("/api/v1/threads/" + selected + "/archive", new JSONObject(), "Task archived");
    }

    @Override
    public void onRequestPermissionsResult(
            int requestCode, String[] permissions, int[] grantResults
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == AUDIO_PERMISSION_REQUEST
                && grantResults.length > 0
                && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            onMicPressed();
        }
    }

    private void sendVoiceMessage(String text) {
        String selected = deckView.getSelectedThreadId();
        if (selected == null) {
            deckView.setListening(false, "SELECT A TASK");
            toast("Select a task before dictating");
            return;
        }
        JSONObject body = new JSONObject();
        try {
            body.put("text", text);
        } catch (Exception ignored) {
            return;
        }
        post(
                "/api/v1/threads/" + selected + "/message",
                body,
                "Message sent to Codex",
                () -> showVoiceResult("MESSAGE SENT", "SENT · " + text),
                () -> showVoiceResult("SEND FAILED", text)
        );
    }

    private void post(String path, JSONObject body, String successMessage) {
        post(path, body, successMessage, null, null);
    }

    private void post(
            String path,
            JSONObject body,
            String successMessage,
            Runnable onSuccess,
            Runnable onFailure
    ) {
        networkExecutor.execute(() -> {
            try {
                request(path, "POST", body);
                mainHandler.post(() -> {
                    if (onSuccess != null) {
                        onSuccess.run();
                    }
                    toast(successMessage);
                });
            } catch (Exception error) {
                mainHandler.post(() -> {
                    if (onFailure != null) {
                        onFailure.run();
                    }
                    toast("PC request failed");
                });
            }
        });
    }

    private void showVoiceResult(String label, String caption) {
        int generation = ++voiceFeedbackGeneration;
        deckView.setListening(false, label);
        deckView.setVoiceCaption(caption);
        mainHandler.postDelayed(() -> {
            if (!listening && generation == voiceFeedbackGeneration) {
                deckView.setListening(false, "TAP TO SPEAK");
                deckView.setVoiceCaption("");
            }
        }, 4_000);
    }

    private JSONObject request(String path, String method, JSONObject body) throws Exception {
        List<String> candidates = new ArrayList<>();
        addCandidate(candidates, activeBaseUrl);
        addCandidate(candidates, BuildConfig.AGENT_DECK_LAN_URL);
        addCandidate(candidates, BuildConfig.AGENT_DECK_URL);

        Exception lastError = null;
        for (String base : candidates) {
            try {
                JSONObject response = requestFrom(base, path, method, body);
                activeBaseUrl = base;
                return response;
            } catch (Exception error) {
                lastError = error;
                if (base.equals(activeBaseUrl)) {
                    activeBaseUrl = null;
                }
            }
        }
        if (lastError != null) {
            throw lastError;
        }
        throw new IllegalStateException("No AgentDeck server address configured");
    }

    private static void addCandidate(List<String> candidates, String candidate) {
        if (candidate == null || candidate.isEmpty() || candidates.contains(candidate)) {
            return;
        }
        candidates.add(candidate);
    }

    private JSONObject requestFrom(
            String configuredBase, String path, String method, JSONObject body
    ) throws Exception {
        String base = configuredBase;
        if (base.endsWith("/")) {
            base = base.substring(0, base.length() - 1);
        }
        HttpURLConnection connection = (HttpURLConnection) new URL(base + path).openConnection();
        connection.setRequestMethod(method);
        connection.setConnectTimeout(2_000);
        connection.setReadTimeout(8_000);
        connection.setRequestProperty(
                "Authorization", "Bearer " + BuildConfig.AGENT_DECK_TOKEN
        );
        connection.setRequestProperty("Accept", "application/json");
        if (body != null) {
            byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json");
            connection.setFixedLengthStreamingMode(bytes.length);
            try (OutputStream stream = connection.getOutputStream()) {
                stream.write(bytes);
            }
        }
        int status = connection.getResponseCode();
        InputStream input = status >= 400
                ? connection.getErrorStream()
                : connection.getInputStream();
        StringBuilder json = new StringBuilder();
        if (input != null) {
            try (BufferedReader reader = new BufferedReader(
                    new InputStreamReader(input, StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    json.append(line);
                }
            }
        }
        connection.disconnect();
        if (status < 200 || status >= 300) {
            throw new IllegalStateException("HTTP " + status + ": " + json);
        }
        return json.length() == 0 ? new JSONObject() : new JSONObject(json.toString());
    }

    private String connectionLabel() {
        return BuildConfig.AGENT_DECK_LAN_URL.equals(activeBaseUrl)
                ? "LOCAL LINK"
                : "PRIVATE LINK";
    }

    private void toast(String text) {
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show();
    }

    public static final class TaskModel {
        public final String id;
        public final String title;
        public final String status;
        public final int speedBars;
        public final boolean fast;
        public final boolean computerControl;
        public final boolean highlighted;

        private TaskModel(
                String id,
                String title,
                String status,
                int speedBars,
                boolean fast,
                boolean computerControl,
                boolean highlighted
        ) {
            this.id = id;
            this.title = title;
            this.status = status;
            this.speedBars = speedBars;
            this.fast = fast;
            this.computerControl = computerControl;
            this.highlighted = highlighted;
        }

        static TaskModel from(JSONObject object) {
            return new TaskModel(
                    object.optString("id"),
                    object.optString("title", "Untitled"),
                    object.optString("status", "idle"),
                    object.optInt("speedBars", 2),
                    object.optBoolean("fast"),
                    object.optBoolean("computerControl"),
                    object.optBoolean("highlighted")
            );
        }
    }

    public static final class UsageModel {
        public final int remainingPercent;
        public final int windowMinutes;
        public final long resetsAt;

        private UsageModel(int remainingPercent, int windowMinutes, long resetsAt) {
            this.remainingPercent = remainingPercent;
            this.windowMinutes = windowMinutes;
            this.resetsAt = resetsAt;
        }

        static UsageModel from(JSONObject object) {
            if (object == null || object.isNull("remainingPercent")) {
                return new UsageModel(-1, 0, 0);
            }
            return new UsageModel(
                    object.optInt("remainingPercent", -1),
                    object.optInt("windowMinutes", 0),
                    object.optLong("resetsAt", 0)
            );
        }

        static UsageModel unknown() {
            return new UsageModel(-1, 0, 0);
        }
    }
}
