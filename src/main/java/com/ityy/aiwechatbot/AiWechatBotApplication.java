package com.ityy.aiwechatbot;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;

@SpringBootApplication
@ConfigurationPropertiesScan
public class AiWechatBotApplication {

    public static void main(String[] args) {
        SpringApplication.run(AiWechatBotApplication.class, args);
    }

}
