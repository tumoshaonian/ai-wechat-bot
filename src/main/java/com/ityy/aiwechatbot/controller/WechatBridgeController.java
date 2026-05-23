package com.ityy.aiwechatbot.controller;

import com.ityy.aiwechatbot.dto.ChatRequest;
import com.ityy.aiwechatbot.dto.ChatResponse;
import com.ityy.aiwechatbot.service.ChatService;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/wechat")
public class WechatBridgeController {

    private final ChatService chatService;

    public WechatBridgeController(ChatService chatService) {
        this.chatService = chatService;
    }

    @PostMapping("/reply")
    public ChatResponse reply(@Valid @RequestBody ChatRequest request) {
        return chatService.chat(request);
    }
}
