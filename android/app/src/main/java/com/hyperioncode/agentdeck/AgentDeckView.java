package com.hyperioncode.agentdeck;

import android.annotation.SuppressLint;
import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.LinearGradient;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.graphics.Shader;
import android.os.SystemClock;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowInsets;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Date;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

public final class AgentDeckView extends View {
    public interface Listener {
        void onTaskPressed(MainActivity.TaskModel task);
        void onNewPressed();
        void onMicPressed();
        void onArchivePressed();
    }

    private static final int BACKGROUND = Color.rgb(5, 7, 10);
    private static final float TILE_CORNER_RADIUS_DP = 9f;
    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint textPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final List<RectF> taskRects = new ArrayList<>();
    private final RectF newRect = new RectF();
    private final RectF micRect = new RectF();
    private final RectF archiveRect = new RectF();
    private List<MainActivity.TaskModel> tasks = Collections.emptyList();
    private final Set<String> locallyAcknowledgedThreads = new HashSet<>();
    private MainActivity.UsageModel usage = MainActivity.UsageModel.unknown();
    private Listener listener;
    private String ledMode = "idle";
    private String selectedThreadId;
    private boolean connected;
    private boolean listening;
    private boolean voiceAvailable = true;
    private float voiceLevel;
    private String connectionLabel = "CONNECTING";
    private String voiceLabel = "TAP TO SPEAK";
    private String voiceCaption = "";
    private float density;
    private int systemInsetLeft;
    private int systemInsetRight;

    public AgentDeckView(Context context) {
        super(context);
        density = getResources().getDisplayMetrics().density;
        setBackgroundColor(BACKGROUND);
        setFocusable(true);
        setContentDescription("Agent Deck task monitor");
        setOnApplyWindowInsetsListener((view, insets) -> {
            systemInsetLeft = insets.getSystemWindowInsetLeft();
            systemInsetRight = insets.getSystemWindowInsetRight();
            invalidate();
            return insets;
        });
        requestApplyInsets();
    }

    public void setListener(Listener listener) {
        this.listener = listener;
    }

    public void setState(
            List<MainActivity.TaskModel> tasks,
            String ledMode,
            String selectedThreadId,
            MainActivity.UsageModel usage
    ) {
        this.tasks = new ArrayList<>(tasks);
        this.ledMode = ledMode;
        this.usage = usage;
        boolean selectionExists = false;
        for (MainActivity.TaskModel task : tasks) {
            selectionExists |= task.id.equals(this.selectedThreadId);
            if (!task.highlighted) {
                locallyAcknowledgedThreads.remove(task.id);
            }
        }
        if (!selectionExists && selectedThreadId != null
                && !selectedThreadId.isEmpty()) {
            this.selectedThreadId = selectedThreadId;
        }
        if (!selectionExists && !tasks.isEmpty()
                && (this.selectedThreadId == null
                || !this.selectedThreadId.equals(selectedThreadId))) {
            this.selectedThreadId = tasks.get(0).id;
        }
        invalidate();
    }

    public void setConnection(boolean connected, String label) {
        this.connected = connected;
        this.connectionLabel = label;
        invalidate();
    }

    public void setVoiceAvailable(boolean available) {
        this.voiceAvailable = available;
        invalidate();
    }

    public void setListening(boolean listening, String label) {
        this.listening = listening;
        this.voiceLabel = label;
        if (!listening) {
            this.voiceLevel = 0f;
        }
        invalidate();
    }

    public void setVoiceLevel(float level) {
        this.voiceLevel = level;
        invalidate();
    }

    public void setVoiceCaption(String caption) {
        this.voiceCaption = caption == null ? "" : caption;
        invalidate();
    }

    public String getSelectedThreadId() {
        return selectedThreadId;
    }

    public void selectThread(String threadId) {
        selectedThreadId = threadId;
        locallyAcknowledgedThreads.add(threadId);
        invalidate();
    }

    @Override
    @SuppressLint("DrawAllocation")
    protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        int width = getWidth();
        int height = getHeight();
        float pad = dp(16);
        float contentLeft = systemInsetLeft;
        float contentRight = width - systemInsetRight;

        paint.setShader(new LinearGradient(
                0, 0, width, height,
                Color.rgb(8, 16, 25), BACKGROUND,
                Shader.TileMode.CLAMP
        ));
        canvas.drawRect(0, 0, width, height, paint);
        paint.setShader(null);

        drawAmbientBar(canvas, contentLeft, contentRight);
        drawHeader(canvas, contentLeft, contentRight);

        boolean landscape = width > height;
        int columns = landscape ? 5 : 3;
        int rows = landscape ? 3 : 5;
        float headerBottom = dp(84);
        float controlsHeight = landscape ? dp(94) : dp(126);
        float gridBottom = height - controlsHeight;
        float gap = dp(9);
        float availableWidth = contentRight - contentLeft
                - pad * 2 - gap * (columns - 1);
        float availableHeight = gridBottom - headerBottom - gap * (rows - 1);
        float cellWidth = availableWidth / columns;
        float cellHeight = availableHeight / rows;

        taskRects.clear();
        boolean animate = false;
        for (int slot = 0; slot < 15; slot++) {
            int row = slot / columns;
            int column = slot % columns;
            RectF rect = new RectF(
                    contentLeft + pad + column * (cellWidth + gap),
                    headerBottom + row * (cellHeight + gap),
                    contentLeft + pad + column * (cellWidth + gap) + cellWidth,
                    headerBottom + row * (cellHeight + gap) + cellHeight
            );
            taskRects.add(rect);
            if (slot == 14) {
                drawUsage(canvas, rect);
                continue;
            }
            MainActivity.TaskModel task = slot < tasks.size() ? tasks.get(slot) : null;
            drawTask(canvas, rect, task);
            animate |= task != null
                    && task.highlighted
                    && !locallyAcknowledgedThreads.contains(task.id);
        }

        drawControls(
                canvas, contentLeft, contentRight, height, controlsHeight, landscape
        );
        if (animate || listening) {
            postInvalidateDelayed(35);
        }
    }

    private void drawAmbientBar(
            Canvas canvas, float contentLeft, float contentRight
    ) {
        int color;
        switch (ledMode) {
            case "approval": color = Color.rgb(255, 40, 55); break;
            case "control": color = Color.rgb(80, 190, 255); break;
            case "solving": color = Color.rgb(0, 42, 130); break;
            case "done": color = Color.rgb(25, 225, 105); break;
            default: color = Color.rgb(12, 25, 42);
        }
        paint.setColor(color);
        paint.setShadowLayer(dp(12), 0, dp(2), color);
        setLayerType(LAYER_TYPE_SOFTWARE, paint);
        canvas.drawRoundRect(
                new RectF(
                        contentLeft + dp(16), dp(10),
                        contentRight - dp(16), dp(15)
                ),
                dp(3), dp(3), paint
        );
        paint.clearShadowLayer();
    }

    private void drawHeader(
            Canvas canvas, float contentLeft, float contentRight
    ) {
        textPaint.setTypeface(android.graphics.Typeface.create(
                "sans-serif-condensed", android.graphics.Typeface.BOLD
        ));
        textPaint.setTextSize(dp(22));
        textPaint.setLetterSpacing(0.14f);
        textPaint.setColor(Color.rgb(242, 247, 255));
        canvas.drawText("AGENT DECK", contentLeft + dp(18), dp(48), textPaint);

        paint.setColor(connected ? Color.rgb(40, 230, 120) : Color.rgb(255, 70, 78));
        canvas.drawCircle(contentRight - dp(113), dp(42), dp(4), paint);
        textPaint.setTypeface(android.graphics.Typeface.create(
                "sans-serif", android.graphics.Typeface.BOLD
        ));
        textPaint.setLetterSpacing(0.08f);
        textPaint.setTextSize(dp(9));
        textPaint.setColor(Color.rgb(145, 158, 174));
        canvas.drawText(
                connectionLabel, contentRight - dp(104), dp(45), textPaint
        );
        textPaint.setLetterSpacing(0f);
    }

    private void drawTask(
            Canvas canvas, RectF rect, MainActivity.TaskModel task
    ) {
        float radius = dp(TILE_CORNER_RADIUS_DP);
        if (task == null) {
            paint.setColor(Color.rgb(7, 10, 14));
            canvas.drawRoundRect(rect, radius, radius, paint);
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeWidth(dp(1));
            paint.setColor(Color.rgb(20, 27, 35));
            canvas.drawRoundRect(rect, radius, radius, paint);
            paint.setStyle(Paint.Style.FILL);
            return;
        }

        int background;
        int accent;
        int textColor = Color.WHITE;
        switch (task.status) {
            case "active":
                background = Color.rgb(24, 53, 72);
                accent = Color.rgb(95, 200, 255);
                break;
            case "wait":
                background = Color.rgb(92, 10, 18);
                accent = Color.rgb(255, 55, 70);
                break;
            case "done":
                background = Color.rgb(8, 72, 35);
                accent = Color.rgb(35, 225, 105);
                break;
            default:
                background = Color.rgb(24, 27, 33);
                accent = Color.rgb(95, 103, 115);
        }

        paint.setColor(background);
        canvas.drawRoundRect(rect, radius, radius, paint);
        boolean highlighted = task.highlighted
                && !locallyAcknowledgedThreads.contains(task.id);
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(dp(highlighted ? 3.5f : 1.2f));
        if (highlighted) {
            float highlightPulse = 0.5f + 0.5f * (float) Math.sin(
                    SystemClock.uptimeMillis() / 636.62
            );
            paint.setColor(blend(
                    Color.rgb(18, 62, 82),
                    Color.rgb(160, 235, 255),
                    highlightPulse
            ));
        } else {
            paint.setColor(accent);
        }
        canvas.drawRoundRect(rect, radius, radius, paint);
        paint.setStyle(Paint.Style.FILL);

        if (task.id.equals(selectedThreadId)) {
            paint.setColor(Color.rgb(95, 200, 255));
            canvas.drawCircle(
                    rect.centerX(), rect.bottom - dp(7), dp(2.5f), paint
            );
        }
        drawSpeedBadge(canvas, rect, task.speedBars);
        if (task.fast) {
            drawBolt(canvas, rect);
        }
        if (task.computerControl) {
            paint.setColor(Color.rgb(0, 38, 115));
            canvas.drawCircle(rect.centerX(), rect.top + dp(10), dp(3), paint);
        }
        drawTitle(canvas, rect, task.title, textColor);
    }

    private void drawUsage(Canvas canvas, RectF rect) {
        float radius = dp(TILE_CORNER_RADIUS_DP);
        int accent;
        if (usage.remainingPercent < 0) {
            accent = Color.rgb(95, 103, 115);
        } else if (usage.remainingPercent <= 20) {
            accent = Color.rgb(255, 70, 78);
        } else if (usage.remainingPercent <= 40) {
            accent = Color.rgb(255, 190, 55);
        } else {
            accent = Color.rgb(80, 190, 255);
        }

        paint.setColor(Color.rgb(6, 13, 21));
        canvas.drawRoundRect(rect, radius, radius, paint);
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(dp(1.2f));
        paint.setColor(Color.rgb(31, 49, 65));
        canvas.drawRoundRect(rect, radius, radius, paint);

        float ringRadius = Math.min(rect.width(), rect.height()) * 0.34f;
        RectF ring = new RectF(
                rect.centerX() - ringRadius,
                rect.centerY() - ringRadius - dp(2),
                rect.centerX() + ringRadius,
                rect.centerY() + ringRadius - dp(2)
        );
        paint.setStrokeWidth(dp(4));
        paint.setColor(Color.rgb(31, 49, 65));
        canvas.drawArc(ring, 0, 360, false, paint);
        if (usage.remainingPercent > 0) {
            paint.setStrokeCap(Paint.Cap.ROUND);
            paint.setColor(accent);
            canvas.drawArc(
                    ring, -90, 360f * usage.remainingPercent / 100f,
                    false, paint
            );
            paint.setStrokeCap(Paint.Cap.BUTT);
        }
        paint.setStyle(Paint.Style.FILL);

        textPaint.setTextAlign(Paint.Align.CENTER);
        textPaint.setTypeface(android.graphics.Typeface.create(
                "sans-serif", android.graphics.Typeface.BOLD
        ));
        textPaint.setTextSize(dp(18));
        textPaint.setColor(Color.rgb(245, 249, 255));
        String percent = usage.remainingPercent < 0
                ? "--%" : usage.remainingPercent + "%";
        canvas.drawText(percent, rect.centerX(), rect.centerY() + dp(3), textPaint);

        textPaint.setTextSize(dp(8));
        textPaint.setLetterSpacing(0.07f);
        textPaint.setColor(accent);
        canvas.drawText(
                usageLabel(usage.resetsAt),
                rect.centerX(), rect.centerY() + ringRadius + dp(13), textPaint
        );
        textPaint.setLetterSpacing(0f);
        textPaint.setTextAlign(Paint.Align.LEFT);
    }

    private String usageLabel(long resetsAt) {
        if (resetsAt <= 0) {
            return "RESET";
        }
        return new SimpleDateFormat("M/d", Locale.getDefault())
                .format(new Date(resetsAt * 1_000L));
    }

    private void drawSpeedBadge(Canvas canvas, RectF rect, int bars) {
        float left = rect.left + dp(5);
        float top = rect.top + dp(5);
        paint.setColor(Color.rgb(10, 20, 34));
        canvas.drawCircle(left + dp(8), top + dp(8), dp(8), paint);
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(dp(1));
        paint.setColor(Color.rgb(110, 190, 255));
        canvas.drawCircle(left + dp(8), top + dp(8), dp(8), paint);
        paint.setStyle(Paint.Style.FILL);
        for (int index = 0; index < 4; index++) {
            float barHeight = dp(3 + index * 2);
            paint.setColor(index < bars
                    ? Color.rgb(235, 244, 255)
                    : Color.rgb(55, 72, 90));
            float x = left + dp(4 + index * 3);
            canvas.drawRect(
                    x, top + dp(13) - barHeight,
                    x + dp(1.5f), top + dp(13), paint
            );
        }
    }

    private void drawBolt(Canvas canvas, RectF rect) {
        float right = rect.right - dp(5);
        float top = rect.top + dp(4);
        Path bolt = new Path();
        bolt.moveTo(right - dp(5), top);
        bolt.lineTo(right - dp(12), top + dp(9));
        bolt.lineTo(right - dp(8), top + dp(9));
        bolt.lineTo(right - dp(11), top + dp(18));
        bolt.lineTo(right, top + dp(7));
        bolt.lineTo(right - dp(5), top + dp(7));
        bolt.close();
        paint.setColor(Color.rgb(255, 225, 70));
        canvas.drawPath(bolt, paint);
    }

    private void drawTitle(Canvas canvas, RectF rect, String title, int color) {
        float maxWidth = rect.width() - dp(16);
        textPaint.setTypeface(android.graphics.Typeface.create(
                "sans-serif", android.graphics.Typeface.BOLD
        ));
        textPaint.setTextSize(dp(11));
        textPaint.setTextAlign(Paint.Align.CENTER);
        textPaint.setColor(color);
        List<String> lines = wrap(title.replace('_', ' '), maxWidth, 3);
        float lineHeight = dp(13);
        float startY = rect.centerY() - (lines.size() - 1) * lineHeight / 2f
                + dp(4);
        for (int index = 0; index < lines.size(); index++) {
            canvas.drawText(
                    lines.get(index), rect.centerX(),
                    startY + index * lineHeight, textPaint
            );
        }
        textPaint.setTextAlign(Paint.Align.LEFT);
    }

    private List<String> wrap(String value, float width, int maxLines) {
        String[] words = value.trim().split("\\s+");
        List<String> lines = new ArrayList<>();
        String current = "";
        for (String word : words) {
            String candidate = current.isEmpty() ? word : current + " " + word;
            if (textPaint.measureText(candidate) <= width) {
                current = candidate;
            } else {
                if (!current.isEmpty()) {
                    lines.add(current);
                }
                current = ellipsize(word, width);
                if (lines.size() == maxLines - 1) {
                    break;
                }
            }
        }
        if (!current.isEmpty() && lines.size() < maxLines) {
            lines.add(current);
        }
        if (lines.isEmpty()) {
            lines.add("Untitled");
        }
        if (lines.size() == maxLines && words.length > maxLines) {
            lines.set(maxLines - 1, ellipsize(lines.get(maxLines - 1) + "...", width));
        }
        return lines;
    }

    private String ellipsize(String value, float width) {
        if (textPaint.measureText(value) <= width) {
            return value;
        }
        String text = value;
        while (text.length() > 1 && textPaint.measureText(text + "...") > width) {
            text = text.substring(0, text.length() - 1);
        }
        return text + "...";
    }

    private void drawControls(
            Canvas canvas,
            float contentLeft,
            float contentRight,
            int height,
            float controlsHeight,
            boolean landscape
    ) {
        float centerY = height - controlsHeight / 2f + (landscape ? dp(6) : 0);
        float radius = landscape ? dp(28) : dp(31);
        float contentWidth = contentRight - contentLeft;
        float spacing = landscape
                ? dp(92) : Math.min(dp(102), contentWidth / 3.5f);
        float centerX = (contentLeft + contentRight) / 2f;
        newRect.set(
                centerX - spacing - radius, centerY - radius,
                centerX - spacing + radius, centerY + radius
        );
        micRect.set(
                centerX - radius, centerY - radius,
                centerX + radius, centerY + radius
        );
        archiveRect.set(
                centerX + spacing - radius, centerY - radius,
                centerX + spacing + radius, centerY + radius
        );

        drawControl(canvas, newRect, "+", "NEW", false, true);
        drawMicControl(canvas);
        drawControl(canvas, archiveRect, "×", "ARCHIVE", false, selectedThreadId != null);

        if (!voiceCaption.isEmpty()) {
            textPaint.setTextAlign(Paint.Align.CENTER);
            textPaint.setTextSize(dp(10));
            textPaint.setColor(Color.rgb(145, 158, 174));
            canvas.drawText(
                    ellipsize(voiceCaption, contentWidth - dp(40)),
                    centerX, centerY - radius - dp(12), textPaint
            );
            textPaint.setTextAlign(Paint.Align.LEFT);
        }
    }

    private void drawMicControl(Canvas canvas) {
        float pulse = listening
                ? 0.5f + 0.5f * (float) Math.sin(SystemClock.uptimeMillis() / 160.0)
                : 0f;
        int color = voiceAvailable
                ? blend(Color.rgb(18, 51, 74), Color.rgb(90, 210, 255), pulse)
                : Color.rgb(35, 38, 43);
        paint.setColor(color);
        canvas.drawOval(micRect, paint);
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(dp(2));
        paint.setColor(listening ? Color.WHITE : Color.rgb(95, 200, 255));
        canvas.drawOval(micRect, paint);
        paint.setStyle(Paint.Style.FILL);

        float cx = micRect.centerX();
        float cy = micRect.centerY() - dp(5);
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(dp(2));
        paint.setStrokeCap(Paint.Cap.ROUND);
        paint.setColor(Color.WHITE);
        canvas.drawRoundRect(
                new RectF(cx - dp(5), cy - dp(10), cx + dp(5), cy + dp(5)),
                dp(5), dp(5), paint
        );
        canvas.drawArc(
                new RectF(cx - dp(9), cy - dp(4), cx + dp(9), cy + dp(11)),
                0, 180, false, paint
        );
        canvas.drawLine(cx, cy + dp(11), cx, cy + dp(16), paint);
        canvas.drawLine(cx - dp(5), cy + dp(16), cx + dp(5), cy + dp(16), paint);
        paint.setStrokeCap(Paint.Cap.BUTT);
        paint.setStyle(Paint.Style.FILL);
        drawControlLabel(canvas, micRect, voiceLabel);
    }

    private void drawControl(
            Canvas canvas,
            RectF rect,
            String symbol,
            String label,
            boolean active,
            boolean enabled
    ) {
        paint.setColor(enabled ? Color.rgb(17, 23, 31) : Color.rgb(10, 12, 16));
        canvas.drawOval(rect, paint);
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(dp(1.5f));
        paint.setColor(enabled ? Color.rgb(73, 90, 108) : Color.rgb(35, 40, 47));
        canvas.drawOval(rect, paint);
        paint.setStyle(Paint.Style.FILL);
        textPaint.setTextAlign(Paint.Align.CENTER);
        textPaint.setTypeface(android.graphics.Typeface.create(
                "sans-serif", android.graphics.Typeface.NORMAL
        ));
        textPaint.setTextSize(dp(27));
        textPaint.setColor(enabled ? Color.WHITE : Color.rgb(70, 75, 82));
        canvas.drawText(symbol, rect.centerX(), rect.centerY() + dp(7), textPaint);
        textPaint.setTextAlign(Paint.Align.LEFT);
        drawControlLabel(canvas, rect, label);
    }

    private void drawControlLabel(Canvas canvas, RectF rect, String label) {
        textPaint.setTextAlign(Paint.Align.CENTER);
        textPaint.setTypeface(android.graphics.Typeface.create(
                "sans-serif", android.graphics.Typeface.BOLD
        ));
        textPaint.setTextSize(dp(8));
        textPaint.setLetterSpacing(0.09f);
        textPaint.setColor(Color.rgb(126, 141, 158));
        canvas.drawText(label, rect.centerX(), rect.bottom + dp(15), textPaint);
        textPaint.setLetterSpacing(0f);
        textPaint.setTextAlign(Paint.Align.LEFT);
    }

    @Override
    public boolean onTouchEvent(MotionEvent event) {
        if (event.getAction() != MotionEvent.ACTION_UP || listener == null) {
            return true;
        }
        float x = event.getX();
        float y = event.getY();
        int clickableTasks = Math.min(tasks.size(), 14);
        for (int index = 0; index < taskRects.size() && index < clickableTasks; index++) {
            if (taskRects.get(index).contains(x, y)) {
                performClick();
                listener.onTaskPressed(tasks.get(index));
                return true;
            }
        }
        if (newRect.contains(x, y)) {
            performClick();
            listener.onNewPressed();
        } else if (micRect.contains(x, y)) {
            performClick();
            listener.onMicPressed();
        } else if (archiveRect.contains(x, y)) {
            performClick();
            listener.onArchivePressed();
        }
        return true;
    }

    @Override
    public boolean performClick() {
        super.performClick();
        return true;
    }

    private float dp(float value) {
        return value * density;
    }

    private static int blend(int first, int second, float amount) {
        amount = Math.max(0f, Math.min(1f, amount));
        int red = (int) (Color.red(first) * (1 - amount) + Color.red(second) * amount);
        int green = (int) (Color.green(first) * (1 - amount) + Color.green(second) * amount);
        int blue = (int) (Color.blue(first) * (1 - amount) + Color.blue(second) * amount);
        return Color.rgb(red, green, blue);
    }
}
