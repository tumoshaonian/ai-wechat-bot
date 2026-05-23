package com.ityy.aiwechatbot.dto;

import java.util.List;

public record OpenAiChatCompletionResponse(
        List<Choice> choices
) {
    public record Choice(Message message) {
    }

    public record Message(String role, String content) {
    }
}
