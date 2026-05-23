package com.ityy.aiwechatbot.dto;

import jakarta.validation.constraints.NotBlank;

public record ChatRequest(
        @NotBlank(message = "message 不能为空")
        String message,
        String sessionId,
        String fromUser,
        String chatName,
        Boolean groupChat,
        String source
) {
}
