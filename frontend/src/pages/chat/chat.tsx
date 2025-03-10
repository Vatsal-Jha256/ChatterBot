import { ChatInput } from "@/components/custom/chatinput";
import { PreviewMessage, ThinkingMessage, StreamingMessage } from "../../components/custom/message";
import { useScrollToBottom } from '@/components/custom/use-scroll-to-bottom';
import { useState, useEffect, useRef } from "react";
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
  const [isStreaming, setIsStreaming] = useState<boolean>(false);
  const [pendingCorrelationId, setPendingCorrelationId] = useState<string | null>(null);
  const eventSourceRef = useRef<EventSource | null>(null);
  const [currentStreamMessage, setCurrentStreamMessage] = useState<string>("");
  const [userId] = useState<string>(() => {
    const storedId = localStorage.getItem('userId') || uuidv4();
    localStorage.setItem('userId', storedId);
    return storedId;
  });

  // Clean up event source on component unmount
  useEffect(() => {
    return () => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
      }
    };
  }, []);

  // Polling for non-streaming responses
  useEffect(() => {
    if (!pendingCorrelationId || isStreaming) return;

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
  }, [pendingCorrelationId, isStreaming]);


const handleStreamResponse = (correlationId: string) => {
  setIsStreaming(true);
  setCurrentStreamMessage("");
  const messageId = uuidv4();
  
  // Close any existing event source
  if (eventSourceRef.current) {
    eventSourceRef.current.close();
  }
  
  const eventSource = new EventSource(`${API_URL}/stream/${correlationId}`);
  eventSourceRef.current = eventSource;
  
  eventSource.onmessage = (event) => {
    try {
      // Handle the [DONE] marker separately
      if (event.data === "[DONE]") {
        eventSource.close();
        setIsLoading(false);
        setIsStreaming(false);
        
        // Add the final streamed message to the messages array
        setMessages(prev => [...prev, {
          content: formatResponseText(currentStreamMessage),
          role: "assistant",
          id: messageId
        }]);
        setCurrentStreamMessage("");
        return;
      }
      
      const data = JSON.parse(event.data);
      
      // Handle the output format from LLM streaming
      if (data.output !== undefined) {
        // Only add the actual output text, not the metadata
        setCurrentStreamMessage(prev => prev + data.output);
      } 
      // Also keep supporting token format for backward compatibility
      else if (data.token !== undefined) {
        // Only add the actual token text, not the metadata
        setCurrentStreamMessage(prev => prev + data.token);
      }
      
      // Handle errors
      if (data.error) {
        setMessages(prev => [...prev, {
          content: data.error || "An error occurred during streaming",
          role: "assistant",
          id: uuidv4()
        }]);
        setIsLoading(false);
        setIsStreaming(false);
        eventSource.close();
      }
      
      // Check if we're done via finish_reason
      if (data.finish_reason === "stop" || data.finish_reason === "length") {
        eventSource.close();
        setIsLoading(false);
        setIsStreaming(false);
        
        // Add the final message to the messages array
        setMessages(prev => [...prev, {
          content: formatResponseText(currentStreamMessage),
          role: "assistant",
          id: messageId
        }]);
        setCurrentStreamMessage("");
      }
    } catch (error) {
      console.error('Stream parsing error:', error);
    }
  };

  // Error handling for EventSource
  eventSource.onerror = (error) => {
    console.error('EventSource error:', error);
    eventSource.close();
    setIsLoading(false);
    setIsStreaming(false);
    
    // If we have partial content, save it
    if (currentStreamMessage) {
      setMessages(prev => [...prev, {
        content: formatResponseText(currentStreamMessage),
        role: "assistant",
        id: messageId
      }]);
      setCurrentStreamMessage("");
    }
  };
};

// Then also update the formatResponseText function:
const formatResponseText = (text: string) => {
  // First remove any JSON fragments that might have leaked through
  let cleaned = text.replace(/\{"output":.*?\}/g, '')
                    .replace(/\{"token":.*?\}/g, '')
                    .replace(/\{"prompt_tokens":.*?\}/g, '')
                    .replace(/\{"finish_reason":.*?\}/g, '');
                    
  // Then do regular formatting
  let formatted = cleaned.replace(/\\n/g, '\n');
  formatted = formatted.replace(/\n-/g, '\n\n-');
  formatted = formatted.replace(/```(\w+)\n/g, '```$1\n\n');
  return formatted;
};


  async function handleSubmit(text?: string) {
    const rawText = text || question;
    const messageText = sanitizeInput(rawText);
    if (!messageText || isLoading) return;
  
    setIsLoading(true);
    const traceId = uuidv4();
  
    // Add user message to the messages array
    setMessages(prev => [...prev, { content: messageText, role: "user", id: traceId }]);
    setQuestion("");
  
    try {
      const response = await fetch(`${API_URL}/stream-chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-API-Key': API_KEY
        },
        body: JSON.stringify({
          user_id: userId,
          message: messageText,
          context: messages.slice(-5).map(m => m.content),
          stream: true, // Always request streaming
          max_tokens: 256,
          temperature: 0.7
        })
      });
  
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      
      const data = await response.json();
      
      if (data.correlation_id) {
        setPendingCorrelationId(data.correlation_id);
        // If the endpoint supports streaming, use it
        if (data.stream_endpoint || data.status === "streaming") {
          handleStreamResponse(data.correlation_id);
        }
      } else if (data.response) {
        // Direct response
        setMessages(prev => [...prev, {
          content: formatResponseText(data.response),
          role: "assistant",
          id: uuidv4()
        }]);
        setIsLoading(false);
      }
    } catch (error) {
      console.error('Chat error:', error);
      setMessages(prev => [...prev, {
        content: "Sorry, there was an error processing your request.",
        role: "assistant",
        id: uuidv4()
      }]);
      setIsLoading(false);
      setIsStreaming(false);
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
        {isStreaming && currentStreamMessage && (
          <StreamingMessage content={currentStreamMessage} />
        )}
        {isLoading && !isStreaming && <ThinkingMessage />}
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