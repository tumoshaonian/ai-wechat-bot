package com.ityy.aiwechatbot.dto;

public record ChatResponse(
        String userMessage,
        String reply,
        String model,
        String sessionId,
        String fromUser,
        String chatName,
        Boolean groupChat,
        String source
) {
}
