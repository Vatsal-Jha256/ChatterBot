import { motion } from 'framer-motion';
import { MessageCircle, BotIcon } from 'lucide-react';

export const Overview = () => {
  return (
    <>
      <motion.div
        key="overview"
        className="max-w-3xl mx-auto md:mt-20 px-6"
        initial={{ opacity: 0, scale: 0.98 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.98 }}
        transition={{ delay: 0.75 }}
      >
        <div className="rounded-xl p-8 flex flex-col gap-6 text-center max-w-xl mx-auto bg-white shadow-xl text-black">
          <p className="flex flex-row justify-center gap-6 items-center mb-6">
            <BotIcon size={48} className="text-black" />
            <span className="text-3xl font-semibold">+</span>
            <MessageCircle size={48} className="text-black" />
          </p>
          <p className="text-xl font-semibold mb-3">
            Welcome to <strong className="font-extrabold">ChatterBot!</strong>
          </p>
          <p className="text-lg text-gray-700">
            A scalable LLM system for production.<br />
            Simple, powerful, and designed for the future of AI-driven conversations.
          </p>
        </div>
      </motion.div>
    </>
  );
};
