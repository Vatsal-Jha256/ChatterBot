import { ChatInput } from "@/components/custom/chatinput";
import { PreviewMessage, ThinkingMessage } from "../../components/custom/message";
import { useScrollToBottom } from '@/components/custom/use-scroll-to-bottom';
import { useState, useEffect } from "react";
import { message } from "../../interfaces/interfaces";
import { Overview } from "@/components/custom/overview";
import { Header } from "@/components/custom/header";
import { v4 as uuidv4 } from 'uuid';

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8001";
const API_KEY = import.meta.env.VITE_API_KEY || "apitest1729";

const sanitizeInput = (input: string) => {
  return input.replace(/\[[A-Z]+\]/gi, '').trim();
};

// Helper function to format the response text with proper line breaks and markdown
const formatResponseText = (text: string) => {
  // First replace escaped newlines with actual newlines
  let formatted = text.replace(/\\n/g, '\n');
  // Ensure proper spacing for markdown lists and code blocks
  formatted = formatted.replace(/\n-/g, '\n\n-');
  formatted = formatted.replace(/```(\w+)\n/g, '```$1\n\n');
  return formatted;
};



export function Chat() {
  const [messagesContainerRef, messagesEndRef] = useScrollToBottom<HTMLDivElement>();
  const [messages, setMessages] = useState<message[]>([]);
  const [question, setQuestion] = useState<string>("");
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [pendingCorrelationId, setPendingCorrelationId] = useState<string | null>(null);
  const [userId] = useState<string>(() => {
    const storedId = localStorage.getItem('userId') || uuidv4();
    localStorage.setItem('userId', storedId);
    return storedId;
  });

  useEffect(() => {
    if (!pendingCorrelationId) return;

    const intervalId = setInterval(async () => {
      try {
        const response = await fetch(`${API_URL}/check-response/${pendingCorrelationId}`, {
          headers: { 'X-API-Key': API_KEY }
        });
        const data = await response.json();

        if (data.response) {
          // Format the response text before adding it to messages
          const formattedResponse = formatResponseText(data.response);
          
          setMessages(prev => [...prev, {
            content: formattedResponse,
            role: "assistant",
            id: uuidv4()
          }]);
          setPendingCorrelationId(null);
          setIsLoading(false);
          clearInterval(intervalId);
        } else if (data.error) {
          setMessages(prev => [...prev, {
            content: data.error,
            role: "assistant",
            id: uuidv4()
          }]);
          setPendingCorrelationId(null);
          setIsLoading(false);
          clearInterval(intervalId);
        }
      } catch (error) {
        console.error('Polling error:', error);
      }
    }, 1000);

    return () => clearInterval(intervalId);
  }, [pendingCorrelationId]);
  const handleStreamResponse = (correlationId: string) => {
    const eventSource = new EventSource(`${API_URL}/stream/${correlationId}`);
    let accumulated = '';
    
    eventSource.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.token) {
        accumulated += data.token;
        setMessages(prev => {
          const last = prev[prev.length - 1];
          return last.role === 'assistant' 
            ? [...prev.slice(0, -1), {...last, content: accumulated}]
            : [...prev, {content: accumulated, role: 'assistant', id: uuidv4()}];
        });
      }
    };
  
    eventSource.onerror = () => {
      eventSource.close();
      setIsLoading(false);
    };
  };
  async function handleSubmit(text?: string) {
    const rawText = text || question;
    const messageText = sanitizeInput(rawText);
    if (!messageText || isLoading) return;

    setIsLoading(true);
    const traceId = uuidv4();

    setMessages(prev => [...prev, { content: messageText, role: "user", id: traceId }]);
    setQuestion("");

    try {
      const response = await fetch(`${API_URL}/async-chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-API-Key': API_KEY
        },
        body: JSON.stringify({
          user_id: userId,
          message: messageText,
          context: messages.slice(-5).map(m => m.content),
          max_tokens: 256,
          temperature: 0.7
        })
      });

      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      
      const data = await response.json();
      if (data.stream) {
        handleStreamResponse(data.correlation_id);
      }
      setPendingCorrelationId(data.correlation_id);
    } catch (error) {
      console.error('Chat error:', error);
      setMessages(prev => [...prev, {
        content: "Sorry, there was an error processing your request.",
        role: "assistant",
        id: uuidv4()
      }]);
      setIsLoading(false);
    }
  }

  return (
    <div className="flex flex-col min-w-0 h-dvh bg-background">
      <Header/>
      <div className="flex flex-col min-w-0 gap-6 flex-1 overflow-y-scroll pt-4" ref={messagesContainerRef}>
        {messages.length === 0 && <Overview />}
        {messages.map((message) => (
          <PreviewMessage key={message.id} message={message} />
        ))}
        {isLoading && <ThinkingMessage />}
        <div ref={messagesEndRef} className="shrink-0 min-w-[24px] min-h-[24px]"/>
      </div>
      <div className="flex mx-auto px-4 bg-background pb-4 md:pb-6 gap-2 w-full md:max-w-3xl">
        <ChatInput  
          question={question}
          setQuestion={setQuestion}
          onSubmit={handleSubmit}
          isLoading={isLoading}
        />
      </div>
    </div>
  );
}