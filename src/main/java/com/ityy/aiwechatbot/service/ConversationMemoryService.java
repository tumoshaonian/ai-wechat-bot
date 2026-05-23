package com.ityy.aiwechatbot.service;

import com.ityy.aiwechatbot.dto.OpenAiChatCompletionRequest;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

@Service
public class ConversationMemoryService {

    private static final int MAX_HISTORY_MESSAGES = 12;

    private final Map<String, Deque<OpenAiChatCompletionRequest.Message>> conversationStore = new ConcurrentHashMap<>();

    public List<OpenAiChatCompletionRequest.Message> getRecentMessages(String sessionId) {
        Deque<OpenAiChatCompletionRequest.Message> history = conversationStore.get(normalizeSessionId(sessionId));
        if (history == null || history.isEmpty()) {
            return List.of();
        }
        return new ArrayList<>(history);
    }

    public void append(String sessionId, String userMessage, String assistantReply) {
        String normalizedSessionId = normalizeSessionId(sessionId);
        Deque<OpenAiChatCompletionRequest.Message> history = conversationStore.computeIfAbsent(
                normalizedSessionId,
                key -> new ArrayDeque<>()
        );

        history.addLast(new OpenAiChatCompletionRequest.Message("user", userMessage));
        history.addLast(new OpenAiChatCompletionRequest.Message("assistant", assistantReply));

        while (history.size() > MAX_HISTORY_MESSAGES) {
            history.removeFirst();
        }
    }

    private String normalizeSessionId(String sessionId) {
        if (!StringUtils.hasText(sessionId)) {
            return "default";
        }
        return sessionId.trim();
    }
}
