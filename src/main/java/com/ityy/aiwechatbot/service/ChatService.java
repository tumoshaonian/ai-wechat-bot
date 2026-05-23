package com.ityy.aiwechatbot.service;

import com.ityy.aiwechatbot.config.AiChatProperties;
import com.ityy.aiwechatbot.dto.ChatRequest;
import com.ityy.aiwechatbot.dto.ChatResponse;
import com.ityy.aiwechatbot.dto.OpenAiChatCompletionRequest;
import com.ityy.aiwechatbot.dto.OpenAiChatCompletionResponse;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;
import org.springframework.web.client.RestClient;

import java.util.ArrayList;
import java.util.List;

@Service
public class ChatService {

    private final RestClient restClient;
    private final AiChatProperties aiChatProperties;
    private final ConversationMemoryService conversationMemoryService;

    public ChatService(
            RestClient.Builder restClientBuilder,
            AiChatProperties aiChatProperties,
            ConversationMemoryService conversationMemoryService
    ) {
        this.restClient = restClientBuilder.build();
        this.aiChatProperties = aiChatProperties;
        this.conversationMemoryService = conversationMemoryService;
    }

    public ChatResponse chat(ChatRequest request) {
        validateConfiguration();

        String sessionId = resolveSessionId(request);
        String source = resolveSource(request);
        boolean groupChat = Boolean.TRUE.equals(request.groupChat());
        String prompt = buildPrompt(request, source, groupChat);

        List<OpenAiChatCompletionRequest.Message> messages = new ArrayList<>();
        if (StringUtils.hasText(aiChatProperties.getSystemPrompt())) {
            messages.add(new OpenAiChatCompletionRequest.Message("system", aiChatProperties.getSystemPrompt()));
        }
        messages.addAll(conversationMemoryService.getRecentMessages(sessionId));
        messages.add(new OpenAiChatCompletionRequest.Message("user", prompt));

        OpenAiChatCompletionResponse response = restClient.post()
                .uri(buildChatCompletionsUrl(aiChatProperties.getBaseUrl()))
                .contentType(MediaType.APPLICATION_JSON)
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + aiChatProperties.getApiKey())
                .body(new OpenAiChatCompletionRequest(aiChatProperties.getModel(), messages))
                .retrieve()
                .body(OpenAiChatCompletionResponse.class);

        String reply = extractReply(response);
        conversationMemoryService.append(sessionId, prompt, reply);

        return new ChatResponse(
                request.message(),
                reply,
                aiChatProperties.getModel(),
                sessionId,
                request.fromUser(),
                request.chatName(),
                groupChat,
                source
        );
    }

    private void validateConfiguration() {
        if (!StringUtils.hasText(aiChatProperties.getApiKey())) {
            throw new IllegalStateException("未配置 ai.chat.api-key，请先在 application.properties 或环境变量中设置。");
        }
        if (!StringUtils.hasText(aiChatProperties.getModel())) {
            throw new IllegalStateException("未配置 ai.chat.model。");
        }
    }

    private String buildChatCompletionsUrl(String baseUrl) {
        String normalized = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        if (normalized.endsWith("/chat/completions")) {
            return normalized;
        }
        if (normalized.endsWith("/v1")) {
            return normalized + "/chat/completions";
        }
        return normalized + "/v1/chat/completions";
    }

    private String extractReply(OpenAiChatCompletionResponse response) {
        if (response == null || response.choices() == null || response.choices().isEmpty()) {
            throw new IllegalStateException("模型返回为空，无法生成回复。");
        }
        OpenAiChatCompletionResponse.Choice firstChoice = response.choices().get(0);
        if (firstChoice.message() == null || !StringUtils.hasText(firstChoice.message().content())) {
            throw new IllegalStateException("模型返回内容为空，无法生成回复。");
        }
        return firstChoice.message().content().trim();
    }

    private String resolveSessionId(ChatRequest request) {
        if (StringUtils.hasText(request.sessionId())) {
            return request.sessionId().trim();
        }
        if (StringUtils.hasText(request.chatName())) {
            return "wechat:" + request.chatName().trim();
        }
        return "default";
    }

    private String resolveSource(ChatRequest request) {
        if (!StringUtils.hasText(request.source())) {
            return "api";
        }
        return request.source().trim();
    }

    private String buildPrompt(ChatRequest request, String source, boolean groupChat) {
        if ("wechat".equalsIgnoreCase(source) || StringUtils.hasText(request.fromUser()) || StringUtils.hasText(request.chatName())) {
            StringBuilder builder = new StringBuilder();
            builder.append("来源: ").append(source).append('\n');
            if (StringUtils.hasText(request.chatName())) {
                builder.append("会话: ").append(request.chatName().trim()).append('\n');
            }
            if (StringUtils.hasText(request.fromUser())) {
                builder.append("发送者: ").append(request.fromUser().trim()).append('\n');
            }
            builder.append("是否群聊: ").append(groupChat ? "是" : "否").append('\n');
            builder.append("用户消息: ").append(request.message());
            return builder.toString();
        }
        return request.message();
    }
}
