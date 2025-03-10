// In StreamingMessage.tsx or component file
import { motion } from 'framer-motion';
import { cx } from 'classix';
import { SparklesIcon } from './icons';
import { Markdown } from './markdown';
// Removed unused useState and useEffect imports

// Define a proper message type
interface Message {
  role: 'user' | 'assistant';
  content: string;
}




export const StreamingMessage = ({ content }: { content: string }) => {
  // Clean up content to make sure we're not displaying raw JSON
  const cleanContent = React.useMemo(() => {
    // Remove any JSON-like fragments that might have slipped through
    return content
      .replace(/\{"output":.*?\}/g, '')
      .replace(/\{"token":.*?\}/g, '')
      .replace(/\{"prompt_tokens":.*?\}/g, '')
      .replace(/\{"finish_reason":.*?\}/g, '');
  }, [content]);
  
  return (
    <motion.div
      className="w-full mx-auto max-w-3xl px-4 group/message"
      initial={{ y: 5, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      data-role="assistant"
    >
      <div
        className={cx(
          "flex gap-4 w-full px-3 py-2 rounded-xl bg-muted max-w-2xl"
        )}
      >
        <div className="size-8 flex items-center rounded-full justify-center ring-1 shrink-0 ring-border">
          <SparklesIcon size={14} />
        </div>
        <div className="flex flex-col w-full">
          <div className="flex flex-col gap-4 text-left">
            <Markdown>{cleanContent}</Markdown>
          </div>
          <motion.div 
            className="h-4 mt-1 flex items-center"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.3 }}
          >
            <motion.div 
              className="w-1 h-4 bg-gray-400 rounded-sm inline-block"
              animate={{ opacity: [1, 0] }}
              transition={{ 
                repeat: Infinity, 
                duration: 0.8,
                repeatType: "reverse"
              }}
            />
          </motion.div>
        </div>
      </div>
    </motion.div>
  );
};
// Enhanced ThinkingMessage with better animation
export const ThinkingMessage = () => {
  return (
    <motion.div
      className="w-full mx-auto max-w-3xl px-4 group/message"
      initial={{ y: 5, opacity: 0 }}
      animate={{ y: 0, opacity: 1, transition: { delay: 0.2 } }}
      data-role="assistant"
    >
      <div
        className={cx(
          "flex gap-4 w-full px-3 py-2 rounded-xl bg-muted max-w-2xl"
        )}
      >
        <div className="size-8 flex items-center rounded-full justify-center ring-1 shrink-0 ring-border">
          <SparklesIcon size={14} />
        </div>
        <div className="flex items-center space-x-2">
          <motion.span 
            className="w-2 h-2 bg-gray-500 rounded-full"
            animate={{ 
              y: [0, -6, 0],
              scale: [1, 1.2, 1]
            }}
            transition={{ 
              repeat: Infinity, 
              duration: 1,
              ease: "easeInOut"
            }}
          />
          <motion.span 
            className="w-2 h-2 bg-gray-500 rounded-full"
            animate={{ 
              y: [0, -6, 0],
              scale: [1, 1.2, 1]
            }}
            transition={{ 
              repeat: Infinity, 
              duration: 1,
              ease: "easeInOut",
              delay: 0.2
            }}
          />
          <motion.span 
            className="w-2 h-2 bg-gray-500 rounded-full"
            animate={{ 
              y: [0, -6, 0],
              scale: [1, 1.2, 1]
            }}
            transition={{ 
              repeat: Infinity, 
              duration: 1,
              ease: "easeInOut",
              delay: 0.4
            }}
          />
        </div>
      </div>
    </motion.div>
  );
};

// Simple MessageActions component to fix the missing reference
const MessageActions = ({ message }: { message: Message }) => {
  return (
    <div className="flex mt-2 space-x-2 text-xs text-gray-500">
      {/* Add your message action buttons here */}
      <button className="hover:text-gray-700">Copy</button>
      <button className="hover:text-gray-700">Regenerate</button>
    </div>
  );
};

export const PreviewMessage = ({ message }: { message: Message }) => {
  return (
    <motion.div
      className="w-full mx-auto max-w-3xl px-4 group/message"
      initial={{ y: 5, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      data-role={message.role}
    >
      <div
        className={cx(
          'group-data-[role=user]/message:bg-zinc-700 dark:group-data-[role=user]/message:bg-muted group-data-[role=user]/message:text-white flex gap-4 group-data-[role=user]/message:px-3 w-full group-data-[role=user]/message:w-fit group-data-[role=user]/message:ml-auto group-data-[role=user]/message:max-w-2xl group-data-[role=user]/message:py-2 rounded-xl'
        )}
      >
        {message.role === 'assistant' && (
          <div className="size-8 flex items-center rounded-full justify-center ring-1 shrink-0 ring-border">
            <SparklesIcon size={14} />
          </div>
        )}
        <div className="flex flex-col w-full">
          {message.content && (
            <div className="flex flex-col gap-4 text-left">
              <Markdown>{message.content}</Markdown>
            </div>
          )}
          {message.role === 'assistant' && (
            <MessageActions message={message} />
          )}
        </div>
      </div>
    </motion.div>
  );
};