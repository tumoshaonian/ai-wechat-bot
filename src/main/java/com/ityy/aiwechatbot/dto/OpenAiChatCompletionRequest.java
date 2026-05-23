package com.ityy.aiwechatbot.dto;

import java.util.List;

public record OpenAiChatCompletionRequest(
        String model,
        List<Message> messages
) {
    public record Message(String role, String content) {
    }
}
