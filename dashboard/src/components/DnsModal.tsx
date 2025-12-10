import {
  Modal,
  ModalOverlay,
  ModalContent,
  ModalHeader,
  ModalCloseButton,
  ModalBody,
  ModalFooter,
  VStack,
  FormControl,
  FormLabel,
  Input,
  Button,
  Text,
} from "@chakra-ui/react";
import { FC, useEffect } from "react";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";

interface DnsModalProps {
  isOpen: boolean;
  onClose: () => void;
  form: any;
  setDnsServers: (data: any[]) => void;
  dnsIndex?: number | null;
  currentDnsData?: any;
}

export const DnsModal: FC<DnsModalProps> = ({ isOpen, onClose, form, setDnsServers, dnsIndex, currentDnsData }) => {
  const { t } = useTranslation();
  const modalForm = useForm({
    defaultValues: {
      address: "",
      domains: "",
      expectIPs: "",
    },
  });

  useEffect(() => {
    if (isOpen && currentDnsData && dnsIndex !== null) {
      // Edit mode - load existing DNS data
      const dnsData = typeof currentDnsData === "object" ? currentDnsData : { address: currentDnsData };
      modalForm.reset({
        address: dnsData.address || "",
        domains: Array.isArray(dnsData.domains) ? dnsData.domains.join(",") : "",
        expectIPs: Array.isArray(dnsData.expectIPs) ? dnsData.expectIPs.join(",") : "",
      });
    } else if (isOpen && dnsIndex === null) {
      // Create mode - reset to empty
      modalForm.reset({
        address: "",
        domains: "",
        expectIPs: "",
      });
    }
  }, [isOpen, currentDnsData, dnsIndex, modalForm]);

  const handleSubmit = modalForm.handleSubmit((data) => {
    const newDns = {
      address: data.address,
      domains: data.domains ? data.domains.split(",").map(d => d.trim()).filter(Boolean) : [],
      expectIPs: data.expectIPs ? data.expectIPs.split(",").map(ip => ip.trim()).filter(Boolean) : [],
    };

    const currentDnsServers = form.getValues("config.dns.servers") || [];
    if (dnsIndex !== null && dnsIndex !== undefined) {
      currentDnsServers[dnsIndex] = newDns;
    } else {
      currentDnsServers.push(newDns);
    }

    form.setValue("config.dns.servers", currentDnsServers, { shouldDirty: true });
    setDnsServers(currentDnsServers);
    onClose();
  });

  return (
    <Modal isOpen={isOpen} onClose={onClose} size="sm">
      <ModalOverlay bg="blackAlpha.300" backdropFilter="blur(10px)" />
      <ModalContent mx="3">
        <ModalHeader pt={6}>
          <Text fontWeight="semibold" fontSize="lg">
            {dnsIndex !== null ? t("pages.xray.dns.edit") : t("pages.xray.dns.add")}
          </Text>
        </ModalHeader>
        <ModalCloseButton mt={3} />
        <ModalBody>
          <form onSubmit={handleSubmit}>
            <VStack spacing={4}>
              <FormControl>
                <FormLabel>{t("pages.xray.dns.address")}</FormLabel>
                <Input {...modalForm.register("address")} size="sm" placeholder="8.8.8.8" />
              </FormControl>
              <FormControl>
                <FormLabel>{t("pages.xray.dns.domains")}</FormLabel>
                <Input {...modalForm.register("domains")} size="sm" placeholder="example.com,*.example.com" />
              </FormControl>
              <FormControl>
                <FormLabel>{t("pages.xray.dns.expectIPs")}</FormLabel>
                <Input {...modalForm.register("expectIPs")} size="sm" placeholder="1.1.1.1,2.2.2.2" />
              </FormControl>
              <Button type="submit" colorScheme="primary" size="sm" w="full">
                {dnsIndex !== null ? t("pages.xray.dns.edit") : t("pages.xray.dns.add")}
              </Button>
            </VStack>
          </form>
        </ModalBody>
      </ModalContent>
    </Modal>
  );
};
